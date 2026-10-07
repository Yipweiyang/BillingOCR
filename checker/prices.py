"""
Contract rate schedules from the price/ folder, and the batch each one
belongs to.

A schedule is the contract's Bill of Quantities: an .xls workbook (RM206),
an .xlsb price list (TR387) or a PDF (TR388, and RM205's whole contract
document). In the BQs only Section B - the provisional quantities for ad hoc
works - is read, because that is what a mastersheet's PQ items refer to;
Section A reuses the same item numbers for planned works at other rates.

    {"contract": "RM206", "region": "NORTH EAST", "source": filename,
     "valid_from": date or None, "valid_to": date or None,
     "items": {"PQ30.1.1": {"rate", "unit", "description"}},
     "warnings": [what in a PDF did not add up, so may be misread]}
"""
import functools
import hashlib
import io
import os
import re
from datetime import date

import fitz
import pdfplumber

from .common import parse_date

PRICE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "price")

CONTRACT_RE = re.compile(r"\b(RM|TR)\s*-?\s*(\d{3})\b", re.I)
REGION_RE = re.compile(r"CONTRACT FOR (NORTH|SOUTH)\s+(EAST|WEST) SECTOR", re.I)
SECTOR_RE = re.compile(r"\b([NS][EW])\d\b")
SECTOR_REGIONS = {"NE": "NORTH EAST", "NW": "NORTH WEST", "SE": "SOUTH EAST", "SW": "SOUTH WEST"}
# "EXTENSION FROM 6 FEBRUARY 2026 TO 5 FEBRUARY 2028"
VALIDITY_RE = re.compile(r"FROM\s+(\d{1,2}\s+[A-Z]+\s+\d{4})\s+TO\s+(\d{1,2}\s+[A-Z]+\s+\d{4})", re.I)
SECTION_B = "PROVISIONAL QUANTITIES FOR AD HOC WORKS"
OPTION_BILL = "OPTION BILL"
# The area a PQ item is for, as its description words it:
# "for locations with each area <= 2m2", "... exceeding 2m2 but n.e. 5m2",
# "... area excceding 5m2" (sic).
AT_MOST_RE = re.compile(r"(?:≤|<=)\s*(\d+(?:\.\d+)?)\s*m2", re.I)
EXCEEDING_RE = re.compile(r"exc+e+ding\s*(\d+(?:\.\d+)?)\s*m2(?:\s*but\s*n\.?\s*e\.?\s*(\d+(?:\.\d+)?)\s*m2)?", re.I)

ITEM_RE = re.compile(r"^\d+(?:\.\d+)+$")
# A sub-item is lettered under the last numbered item: "a)", "a" or "a.".
LETTER_RE = re.compile(r"^([a-z])[.)]?(?:\s+|$)")
# A priced PDF line: "30.1.1 for locations with each area <= 2m2 m2 3,333 32.10 1 06,989.30".
# The amount's digits come out split by stray spaces, so it is matched loosely.
PDF_LINE_RE = re.compile(
    r"^(?P<code>\d+(?:\.\d+)+|[a-z][.)]?)\s+(?P<desc>.+?)\s+(?P<unit>\S+)\s+"
    r"(?P<qty>\d[\d,]*(?:\.\d+)?)\s+(?P<rate>\d[\d,]*\.\d{2})\s+[\d ,]*\.\d{2}$")
NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)+)\s")
# The same line with the quantity or the rate split too - "c) Dash Line Marking
# m 7 ,000.00 2.20 15,400.00", "... m 1 10,000.00 1 .10 121,000.00". The
# figures are taken as one run and told apart once the spaces are out.
PDF_SPLIT_LINE_RE = re.compile(
    r"^(?P<code>\d+(?:\.\d+)+|[a-z][.)]?)\s+(?P<desc>.+?)\s+(?P<unit>[A-Za-z]\S*)\s+(?P<figures>\d[\d ,.]*\.\d{2})$")
THREE_FIGURES_RE = re.compile(r"(\d[\d,]*\.\d{2})(\d[\d,]*\.\d{2})(\d[\d,]*\.\d{2})")
# TR387's schedule marks the rate and the amount with a "$", so each is told
# apart however its digits are split: "a1) for locations with each area <= 2m2
# m2 471 $ 1 49.00 $ 70,179.00". Its sub-items carry a digit ("a1)", "b3)"),
# and a quantity can fall on the line above, leaving none on this one.
PDF_DOLLAR_LINE_RE = re.compile(
    r"^(?P<code>\d+(?:\.\d+)+|[a-z]\d?[.)]?)\s+(?P<desc>.+?)\s+(?P<unit>\S+)\s+"
    r"(?:(?P<qty>\d[\d,]*(?:\.\d+)?)\s+)?\$\s*(?P<rate>\d[\d ,]*\.\d{2})\s+\$\s*[\d ,]*\.\d{2}$")
SUB_ITEM_RE = re.compile(r"[a-z]\d*", re.I)


def split_figures(figures):
    """
    (qty, rate) from a priced line's run of figures, or None. The split is
    only trusted when it is the one the line's own arithmetic bears out:
    quantity times rate is the amount.
    """
    m = THREE_FIGURES_RE.fullmatch(figures.replace(" ", ""))
    if not m:
        return None
    qty, rate, amount = (float(x.replace(",", "")) for x in m.groups())
    return (qty, rate) if abs(qty * rate - amount) <= 0.01 * max(1.0, amount) / 100 + 0.01 else None


def contract_code(text):
    m = CONTRACT_RE.search(text or "")
    return f"{m.group(1).upper()}{m.group(2)}" if m else None


def pq_key(code, letter=""):
    return f"PQ{code}{letter}".upper()


def area_band(description):
    """
    (above, up_to) in m2 for an item priced by the size of the location -
    above is exclusive, up_to inclusive, either may be None - or None for
    an item that names no size.
    """
    m = EXCEEDING_RE.search(description or "")
    if m:
        return float(m.group(1)), float(m.group(2)) if m.group(2) else None
    m = AT_MOST_RE.search(description or "")
    if m:
        return None, float(m.group(1))
    return None


def same_unit(a, b):
    """
    'm2' and 'm²', 'nos' and 'no.' are the same unit. So are m3 and m2:
    every PQ item billed here is an area, and 'm3' on a mastersheet is a
    typing slip for m2, not a volume.
    """
    def norm(u):
        u = re.sub(r"[\s.]", "", (u or "").lower()).replace("²", "2").replace("³", "3")
        if u == "m3":
            return "m2"
        return "no" if u in ("no", "nos", "nr") else u
    return norm(a) == norm(b)


class _Items:
    """Collects priced items, remembering the numbered item a letter belongs to."""

    def __init__(self):
        self.items = {}
        self.parent = None

    def add(self, code, rate, unit, description):
        if ITEM_RE.match(code):
            self.parent = code
            key = pq_key(code)
        else:
            if self.parent is None:
                return
            # "a)" is item a, "a1)" item a1 - PQ30.2.1a1 on a mastersheet.
            key = pq_key(self.parent, SUB_ITEM_RE.match(code).group())
        if rate is not None:
            # The first price wins: a later row with the same number is a
            # misread, not a correction.
            self.items.setdefault(key, {"rate": rate, "unit": unit, "description": description})


# ---------- .xls ----------
def _xls_code(cell, previous):
    """
    An item number as the workbook shows it. A number typed into a numeric
    cell loses its trailing zero - item 16.10 is stored as 16.1 - so a
    number that would step backwards from the item before it gets it back.
    """
    import xlrd

    if cell.ctype == xlrd.XL_CELL_NUMBER:
        value = cell.value
        code = str(int(value)) if float(value).is_integer() else repr(value)
        if previous and "." in code:
            head, _, last = code.rpartition(".")
            p_head, _, p_last = previous.rpartition(".")
            if head == p_head and p_last.isdigit() and int(p_last) >= int(last):
                code = f"{code}0"
        return code
    return " ".join(str(cell.value).split())


def read_xls(data):
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    text = " ".join(str(c.value) for s in book.sheets() for r in range(min(s.nrows, 10)) for c in s.row(r))
    region = REGION_RE.search(text)
    schedule = {"contract": contract_code(text), "region": " ".join(region.groups()).upper() if region else None,
                "valid_from": None, "valid_to": None}

    found = _Items()
    for sheet in book.sheets():
        in_section_b = False
        previous = None
        for r in range(sheet.nrows):
            row = sheet.row(r)
            if len(row) < 5:
                continue
            first = " ".join(str(row[0].value).split())
            if "SECTION" in first.upper():
                in_section_b = False
            if SECTION_B in first.upper():
                in_section_b = True
            if not in_section_b:
                continue

            code = _xls_code(row[0], previous) if first else ""
            description = " ".join(str(row[1].value).split())
            if ITEM_RE.match(code):
                previous = code
            elif not code:
                # "a) With foundation" can sit in the description column.
                m = LETTER_RE.match(description)
                if not m:
                    continue
                code = m.group(1)
            elif not LETTER_RE.match(code):
                continue

            rate = row[4].value if isinstance(row[4].value, float) and row[4].value > 0 else None
            unit = " ".join(str(row[2].value).split()) or None
            found.add(code, rate, unit, description)

    schedule["items"] = found.items
    return schedule


# ---------- .xlsb ----------
# TR387's workbook keeps its rates on a price-list sheet headed
# ITEM / PAGE / DESCRIPTION OF WORKS / UNIT / RATE, each row carrying its
# full code ("PQ30.1.1", "PQ30.2.1a1") rather than a letter under a number.
# The prefix keeps PQ, FSR and SOR items apart, so all three are read - a
# mastersheet bills labour (FSR16.1.1) alongside the PQ works.
# A letter may follow a dot, as in "PQ30.6.b".
PRICED_CODE_RE = re.compile(r"^(?:PQ|FSR)\d+(?:\.\d+)*(?:\.?[A-Za-z]\d*)?$|^SOR\.[A-Z]+\d+$", re.I)


def read_xlsb(data):
    from pyxlsb import open_workbook

    with open_workbook(io.BytesIO(data)) as book:
        sheets = []
        for name in book.sheets:
            with book.get_sheet(name) as sheet:
                sheets.append([[c.v for c in row] for row in sheet.rows()])

    def cell(row, j):
        return " ".join(str(row[j]).split()) if j < len(row) and row[j] is not None else ""

    schedule = {"contract": None, "region": None, "valid_from": None, "valid_to": None, "items": {}}
    for rows in sheets:
        cols = None
        for row in rows:
            upper = [cell(row, j).upper() for j in range(len(row))]
            if cols is None:
                if "ITEM" in upper and "UNIT" in upper and any(c.startswith("RATE") for c in upper):
                    cols = {"code": upper.index("ITEM"), "unit": upper.index("UNIT"),
                            "rate": next(j for j, c in enumerate(upper) if c.startswith("RATE")),
                            "desc": next((j for j, c in enumerate(upper) if c.startswith("DESCRIPTION")), None)}
                    # The contract and region are named in the lines above the header.
                    above = " ".join(" ".join(cell(r, j) for j in range(len(r))) for r in rows[:rows.index(row)])
                    region = REGION_RE.search(above)
                    schedule["contract"] = contract_code(above)
                    schedule["region"] = " ".join(region.groups()).upper() if region else None
                continue
            code = cell(row, cols["code"])
            rate = row[cols["rate"]] if cols["rate"] < len(row) else None
            if PRICED_CODE_RE.match(code) and isinstance(rate, (int, float)) and rate > 0:
                schedule["items"].setdefault(code.upper(), {
                    "rate": float(rate), "unit": cell(row, cols["unit"]) or None,
                    "description": cell(row, cols["desc"]) if cols["desc"] is not None else ""})
        if schedule["items"]:
            break
    return schedule


# ---------- PDF ----------
def read_pdf(data):
    with fitz.open(stream=data, filetype="pdf") as doc:
        first = " ".join(doc[0].get_text().split()) if len(doc) else ""
    region = REGION_RE.search(first)
    validity = VALIDITY_RE.search(first)
    schedule = {
        "contract": contract_code(first),
        "region": " ".join(region.groups()).upper() if region else None,
        "valid_from": parse_date(validity.group(1)) if validity else None,
        "valid_to": parse_date(validity.group(2)) if validity else None,
    }

    # pdfplumber is slow, and RM205's contract document runs to 717 pages,
    # so the Section B pages are picked out by the quicker text layer first.
    # Its option bill repeats Section B's items at the rates of an option
    # the Authority may exercise, which a mastersheet does not bill.
    with fitz.open(stream=data, filetype="pdf") as doc:
        wanted = [i for i, page in enumerate(doc)
                  if SECTION_B in (text := " ".join(page.get_text().upper().split()))
                  and OPTION_BILL not in text]

    # A page with ruled columns is read by column; any other page line by line.
    found = _Items()
    warnings = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(i, _table(pdf.pages[i])) for i in wanted]
        for n, (i, table) in enumerate(pages):
            if table is None:
                _read_lines(pdf.pages[i].extract_text() or "", found)
                continue
            # The item after this page's last one tells whether that one is a heading.
            after = next((row["code"] for _, later in pages[n + 1:n + 2] if later
                          for row in later["rows"] if row["code"]), None)
            label = f"page {i + 1}" + (f" ({table['label']})" if table["label"] else "")
            warnings.extend(f"{label}: {problem}" for problem in _read_table(table, after, found))

    schedule["items"] = found.items
    schedule["warnings"] = warnings
    return schedule


def _read_lines(text, found):
    """A page's priced lines by their pattern, for a page with no ruled columns."""
    for line in text.splitlines():
        line = " ".join(line.split())
        m = PDF_DOLLAR_LINE_RE.match(line)
        if m:
            found.add(m.group("code"), float(re.sub(r"[ ,]", "", m.group("rate"))), m.group("unit"),
                      m.group("desc"))
            continue
        m = PDF_LINE_RE.match(line)
        if m:
            found.add(m.group("code"), float(m.group("rate").replace(",", "")), m.group("unit"),
                      m.group("desc"))
            continue
        m = PDF_SPLIT_LINE_RE.match(line)
        figures = split_figures(m.group("figures")) if m else None
        if figures:
            found.add(m.group("code"), figures[1], m.group("unit"), m.group("desc"))
            continue
        # An unpriced heading still names the item its letters belong to.
        heading = NUMBERED_RE.match(line)
        if heading:
            found.add(heading.group(1), None, None, line)


# ---------- PDF, by column ----------
# A BQ page is a ruled table headed ITEM / DESCRIPTION / UNIT / QTY / RATE /
# AMOUNT. Taking each figure from the column it is printed in, rather than
# from where it falls in a line of text, is what makes a "$" before a rate,
# digits split by stray spaces or a new style of item number not matter.
HEADER_WORDS = {"ITEM": "code", "DESCRIPTION": "desc", "UNIT": "unit", "QTY": "qty", "RATE": "rate",
                "AMOUNT": "amount"}
ROW_CODE_RE = re.compile(r"^(?:\d+(?:\.\d+)*|[a-z]\d?[.)]?)$", re.I)
# "a. UMH workstation" - a sub-item lettered inside the description column.
DESC_CODE_RE = re.compile(r"^([a-z]\d?[.)])\s+(.+)$", re.I)
PAGE_LABEL_RE = re.compile(r"^PQ-\d+$")
SUBTOTAL_RE = re.compile(r"sub-?\s*total", re.I)
LINE_TOLERANCE = 2.5     # points between the middles of words on one line
UNIT_LINE_GAP = 14       # a unit running on to the next line: "per" / "lapping"


def _number(text):
    text = re.sub(r"[$,\s]", "", text or "")
    return float(text) if re.fullmatch(r"\d+(?:\.\d+)?", text) else None


def _table(page):
    """
    {"label": "PQ-6" or None, "rows": [...], "subtotal": amount or None} for
    a page ruled into the BQ's columns, or None for any other page.

    A row is one printed line: {"y", "code", "desc", "unit", "qty", "rate",
    "amount"}, the figures as numbers. An item's figures are not always on
    its own line, so rows are tied to items afterwards, by _read_table().
    """
    edges = sorted([x for r in page.rects if r["height"] > 50 for x in (r["x0"], r["x1"])]
                   + [l["x0"] for l in page.lines if abs(l["x0"] - l["x1"]) < 1 and abs(l["top"] - l["bottom"]) > 50])
    rules = []
    for x in edges:
        if not rules or x - rules[-1] > 3:
            rules.append(x)
    if len(rules) < 2:
        return None

    lines = []
    for w in sorted(page.extract_words(x_tolerance=1.5), key=lambda w: (w["top"] + w["bottom"]) / 2):
        y = (w["top"] + w["bottom"]) / 2
        if lines and abs(lines[-1][0] - y) <= LINE_TOLERANCE:
            lines[-1][1].append(w)
        else:
            lines.append((y, [w]))

    def column(w):
        centre = (w["x0"] + w["x1"]) / 2
        return sum(1 for x in rules if x <= centre)

    label, columns, rows, subtotal = None, None, [], None
    for y, words in lines:
        words.sort(key=lambda w: w["x0"])
        if columns is None:
            text = " ".join(w["text"] for w in words)
            if PAGE_LABEL_RE.match(text):
                label = text
            named = {HEADER_WORDS[w["text"].upper()]: column(w) for w in words if w["text"].upper() in HEADER_WORDS}
            if len(named) == len(HEADER_WORDS) and len(set(named.values())) == len(named):
                columns = {col: name for name, col in named.items()}
            continue
        cells = {}
        for w in words:
            name = columns.get(column(w))
            if name:
                cells.setdefault(name, []).append(w["text"])
        row = {"y": y, "code": " ".join(cells.get("code", [])), "desc": " ".join(cells.get("desc", [])),
               "unit": " ".join(cells.get("unit", [])), "qty": _number("".join(cells.get("qty", []))),
               "rate": _number("".join(cells.get("rate", []))), "amount": _number("".join(cells.get("amount", [])))}
        if row["rate"] is None and row["qty"] and row["amount"]:
            # A stray mark in the cell - RM205 prints one rate as "104 -.23" - is
            # read past only when the line's own arithmetic bears the rate out.
            rate = _number(re.sub(r"[^\d.]", "", "".join(cells.get("rate", []))))
            if rate and abs(row["qty"] * rate - row["amount"]) <= 0.05:
                row["rate"] = rate
        if SUBTOTAL_RE.search(row["desc"]):
            subtotal = row["amount"]
            break
        if not ROW_CODE_RE.match(row["code"]):
            row["code"] = ""
            m = DESC_CODE_RE.match(row["desc"]) if row["rate"] is not None else None
            if m:
                row["code"], row["desc"] = m.groups()
        rows.append(row)
    return {"label": label, "rows": rows, "subtotal": subtotal} if columns else None


def _is_heading(code, after):
    """Whether the item numbered `after` comes under `code`: 30.2 over 30.2.1 or a), c) over c1)."""
    if not after:
        return False
    if code[0].isdigit():
        return not after[0].isdigit() or after.startswith(code + ".")
    if after[0].isdigit():
        return False
    return len(SUB_ITEM_RE.match(code).group()) == 1 and len(SUB_ITEM_RE.match(after).group()) > 1 \
        and after[0].lower() == code[0].lower()


def _read_table(table, after, found):
    """
    Add a page's items to `found`, and return what on the page does not add
    up - so an item missed or misread is said, not left to surface as a PQ
    item "not in the schedule" months later.

    Most items have their figures on their own line. Where a line of figures
    stands alone - above or below its item, as the cell happens to be aligned -
    it lies between two items that do have theirs, and goes to the item there
    still without any: headings, which have items under them, are not priced.
    """
    rows = table["rows"]
    coded = [k for k, row in enumerate(rows) if row["code"]]
    following = {k: rows[coded[n + 1]]["code"] if n + 1 < len(coded) else after for n, k in enumerate(coded)}
    figures = {k: k for k in coded if rows[k]["rate"] is not None}

    def settle(items, loose):
        if not loose:
            return
        leaves = [k for k in items if not _is_heading(rows[k]["code"], following[k])]
        for candidates in (leaves, items):
            if len(candidates) == len(loose):
                figures.update(zip(candidates, loose))
                return

    items, loose = [], []
    for k, row in enumerate(rows):
        if k in figures:
            settle(items, loose)
            items, loose = [], []
        elif row["code"]:
            items.append(k)
        elif row["rate"] is not None:
            loose.append(k)
    settle(items, loose)

    problems, total = [], 0.0
    for k in coded:
        row, priced = rows[k], rows[figures[k]] if k in figures else None
        code = row["code"]
        if not ITEM_RE.match(code) and code[0].isdigit():
            continue                    # "30 FOOTPATHS": a section title
        if priced is None:
            found.add(code, None, None, row["desc"])
            continue
        # A cell's text may sit a line off its item number, like its figures.
        desc = row["desc"] or priced["desc"] or next(
            (r["desc"] for r in rows if r["desc"] and not r["code"] and abs(r["y"] - row["y"]) <= UNIT_LINE_GAP), "")
        j = figures[k] if priced["unit"] else k
        unit, y = rows[j]["unit"], rows[j]["y"]
        for r in rows[j + 1:]:
            if r["code"] or r["rate"] is not None or r["y"] - y > UNIT_LINE_GAP:
                break
            if r["unit"]:
                unit, y = unit + ("" if not unit or unit.endswith("-") else " ") + r["unit"], r["y"]
        found.add(code, priced["rate"], unit or None, desc)

        qty, amount = priced["qty"] if priced["qty"] is not None else row["qty"], priced["amount"]
        total += amount or 0.0
        if qty is not None and amount is not None and abs(qty * priced["rate"] - amount) > 0.005 * amount + 0.05:
            problems.append(f"item {code} shows {qty:g} x {priced['rate']:,.2f}, which is not its amount of "
                            f"{amount:,.2f} - one of the figures may be misread")

    unread = sum(1 for k, row in enumerate(rows) if row["rate"] is not None and k not in figures.values())
    if table["subtotal"] is not None and abs(total - table["subtotal"]) > 0.05:
        problems.append(f"the items read add up to {total:,.2f}, but the page's sub-total is "
                        f"{table['subtotal']:,.2f} - an item may have been missed or misread")
    elif unread:
        problems.append(f"{unread} priced line(s) could not be tied to an item number")
    return problems


# ---------- Loading schedules ----------
READERS = {".pdf": read_pdf, ".xls": read_xls, ".xlsb": read_xlsb}

# Parsed schedules by file content. RM205's 717-page contract document
# takes seconds to read, and the app hands over the same upload on every run.
_parsed = {}


def _read(name, data):
    key = hashlib.sha1(data).hexdigest()
    if key not in _parsed:
        if len(_parsed) >= 16:
            _parsed.pop(next(iter(_parsed)))
        _parsed[key] = READERS[os.path.splitext(name)[1].lower()](data)
    return _parsed[key]


def _collect(files):
    """
    ({contract: [schedule, ...]}, ((filename, why), ...)) from [(filename,
    bytes-or-loader)] - the second is what was not usable, so no file is
    ever skipped without a word.
    """
    schedules, rejected = {}, []
    for name, data in files:
        if os.path.splitext(name)[1].lower() not in READERS:
            rejected.append((name, "is not a supported file type (" + ", ".join(READERS) + ")"))
            continue
        try:
            schedule = _read(name, data() if callable(data) else data)
        except Exception as e:
            rejected.append((name, f"could not be read ({e})"))
            continue
        # The file name is the fallback when the schedule never states its contract.
        contract = schedule["contract"] or contract_code(name)
        if not contract:
            rejected.append((name, "names no contract, and none could be read from its file name"))
            continue
        if not schedule["items"]:
            rejected.append((name, f"has no priced items under '{SECTION_B}'"))
            continue
        schedules.setdefault(contract, []).append({**schedule, "contract": contract, "source": name})

    # Oldest first, so the newest schedule wins where two cover the same day.
    for group in schedules.values():
        group.sort(key=lambda s: (s["valid_from"] or date.min, s["source"]))
    return schedules, tuple(rejected)


def _file_reader(path):
    def read():
        with open(path, "rb") as f:
            return f.read()
    return read


@functools.lru_cache(maxsize=4)
def _load(folder, stamp):
    """_collect() over the price folder; stamp makes a changed folder load again."""
    names = sorted(n for n in os.listdir(folder) if not n.startswith("."))
    return _collect([(n, _file_reader(os.path.join(folder, n))) for n in names])


def _loaded(folder=PRICE_DIR):
    if not os.path.isdir(folder):
        return {}, ()
    stamp = tuple((e.name, e.stat().st_mtime) for e in os.scandir(folder))
    return _load(folder, stamp)


def load_schedules(folder=PRICE_DIR):
    """
    {contract: [schedule, ...]} for every rate schedule in the folder, or {}.

    A contract accumulates schedules - an original and its extensions - so
    each one keeps its own entry rather than the last read silently
    replacing the ones before it.
    """
    return _loaded(folder)[0]


def covers(schedule, day):
    start, end = schedule["valid_from"], schedule["valid_to"]
    return not ((start and day < start) or (end and day > end))


def schedule_for(schedules, completed):
    """
    The schedule that priced work finished on `completed`.

    With none covering that day the work was priced under a schedule the
    folder does not hold, so the closest is returned and check 5 shows its
    rates without judging them.
    """
    if not schedules:
        return None
    if completed is not None:
        covering = [s for s in schedules if covers(s, completed)]
        if covering:
            return covering[-1]
    undated = [s for s in schedules if not s["valid_from"] and not s["valid_to"]]
    return (undated or schedules)[-1]


def price_list_for(master_bytes, price_files=None):
    """
    The rate schedules a mastersheet is billed against:
    {"contract": code or None, "schedules": [...], "rejected": [...],
     "where": what check 5 calls the place schedules came from}.

    price_files is [(filename, bytes)], the schedules a user uploaded with
    the batch - the app's only source, as price files are never committed.
    Without it the price/ folder is read, for batches checked in code.

    A contract may have several, so check 5 picks the one covering each
    line's completion date; "rejected" names the price files that could
    not be used, so they are reported rather than quietly ignored.

    Most mastersheets name their contract (RM205, RM206, TR387). The TR388
    one does not, so a sheet naming none is matched by the region of its
    sectors - NW1 is the North West sector - when exactly one schedule
    covers that region.
    """
    if price_files is None:
        schedules, rejected = _loaded()
        where = "in the price folder"
    else:
        schedules, rejected = _collect(price_files)
        where = "among the uploaded price schedules"
    with fitz.open(stream=master_bytes, filetype="pdf") as doc:
        text = " ".join(page.get_text() for page in doc)

    contract = contract_code(text)
    if contract is None:
        regions = {SECTOR_REGIONS[s] for s in SECTOR_RE.findall(text.upper())}
        matches = [c for c, group in schedules.items()
                   if any(s.get("region") in regions for s in group)]
        if len(matches) == 1:
            contract = matches[0]
    used = schedules.get(contract, [])
    return {"contract": contract, "schedules": used, "rejected": list(rejected), "where": where,
            "warnings": [(s["source"], problem) for s in used for problem in s.get("warnings", ())]}
