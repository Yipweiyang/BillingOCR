"""
Contract rate schedules from the price/ folder, and the batch each one
belongs to.

A schedule is the contract's Bill of Quantities: an .xls workbook (RM206)
or a PDF (TR388). Only Section B - the provisional quantities for ad hoc
works - is read, because that is what a mastersheet's PQ items refer to;
Section A reuses the same item numbers for planned works at other rates.

    {"contract": "RM206", "region": "NORTH EAST", "source": filename,
     "valid_from": date or None, "valid_to": date or None,
     "items": {"PQ30.1.1": {"rate", "unit", "description"}}}
"""
import functools
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

ITEM_RE = re.compile(r"^\d+(?:\.\d+)+$")
# A sub-item is lettered under the last numbered item: "a)", "a" or "a.".
LETTER_RE = re.compile(r"^([a-z])[.)]?(?:\s+|$)")
# A priced PDF line: "30.1.1 for locations with each area <= 2m2 m2 3,333 32.10 1 06,989.30".
# The amount's digits come out split by stray spaces, so it is matched loosely.
PDF_LINE_RE = re.compile(
    r"^(?P<code>\d+(?:\.\d+)+|[a-z][.)]?)\s+(?P<desc>.+?)\s+(?P<unit>\S+)\s+"
    r"(?P<qty>\d[\d,]*(?:\.\d+)?)\s+(?P<rate>\d[\d,]*\.\d{2})\s+[\d ,]*\.\d{2}$")
NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)+)\s")


def contract_code(text):
    m = CONTRACT_RE.search(text or "")
    return f"{m.group(1).upper()}{m.group(2)}" if m else None


def pq_key(code, letter=""):
    return f"PQ{code}{letter}".upper()


def same_unit(a, b):
    """'m2' and 'm²', 'nos' and 'no.' are the same unit."""
    def norm(u):
        u = re.sub(r"[\s.]", "", (u or "").lower()).replace("²", "2").replace("³", "3")
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
            key = pq_key(self.parent, code[0])
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

    found = _Items()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if SECTION_B not in text.upper():
                continue
            for line in text.splitlines():
                line = " ".join(line.split())
                m = PDF_LINE_RE.match(line)
                if m:
                    found.add(m.group("code"), float(m.group("rate").replace(",", "")), m.group("unit"),
                              m.group("desc"))
                    continue
                # An unpriced heading still names the item its letters belong to.
                heading = NUMBERED_RE.match(line)
                if heading:
                    found.add(heading.group(1), None, None, line)

    schedule["items"] = found.items
    return schedule


# ---------- The price folder ----------
def _read(path):
    with open(path, "rb") as f:
        data = f.read()
    return read_pdf(data) if path.lower().endswith(".pdf") else read_xls(data)


@functools.lru_cache(maxsize=4)
def _load(folder, stamp):
    """({contract: [schedule, ...]}, [(filename, why)]) - the second is what was not usable."""
    schedules, rejected = {}, []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith((".pdf", ".xls")):
            continue
        try:
            schedule = _read(os.path.join(folder, name))
        except Exception as e:
            rejected.append((name, f"could not be read ({e})"))
            continue
        # The file name is the fallback when the schedule never states its contract.
        contract = schedule["contract"] or contract_code(name)
        if not contract:
            rejected.append((name, "names no contract, and none could be read from its file name"))
            continue
        if not schedule["items"]:
            rejected.append((name, f"no priced items found under '{SECTION_B}'"))
            continue
        schedules.setdefault(contract, []).append({**schedule, "contract": contract, "source": name})

    # Oldest first, so the newest schedule wins where two cover the same day.
    for group in schedules.values():
        group.sort(key=lambda s: (s["valid_from"] or date.min, s["source"]))
    return schedules, tuple(rejected)


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


def rejected_files(folder=PRICE_DIR):
    """[(filename, why)] for the price files that could not be used."""
    return list(_loaded(folder)[1])


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


def price_list_for(master_bytes, schedules=None):
    """
    The rate schedules a mastersheet is billed against:
    {"contract": code or None, "schedules": [...], "rejected": [...]}.

    A contract may have several, so check 5 picks the one covering each
    line's completion date; "rejected" names the price files that could
    not be used, so they are reported rather than quietly ignored.

    Most mastersheets name their contract (RM205, RM206, TR387). The TR388
    one does not, so a sheet naming none is matched by the region of its
    sectors - NW1 is the North West sector - when exactly one schedule
    covers that region.
    """
    rejected = []
    if schedules is None:
        schedules, rejected = load_schedules(), rejected_files()
    with fitz.open(stream=master_bytes, filetype="pdf") as doc:
        text = " ".join(page.get_text() for page in doc)

    contract = contract_code(text)
    if contract is None:
        regions = {SECTOR_REGIONS[s] for s in SECTOR_RE.findall(text.upper())}
        matches = [c for c, group in schedules.items()
                   if any(s.get("region") in regions for s in group)]
        if len(matches) == 1:
            contract = matches[0]
    return {"contract": contract, "schedules": schedules.get(contract, []), "rejected": rejected}
