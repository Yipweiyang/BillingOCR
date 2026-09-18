"""Reads the mastersheet PDF into {defect ref: {sn, date, jobs}}."""
import io

import fitz
import pdfplumber

from .common import PQ_RE, clip_text, norm_ref, num, parse_date

# Column positions differ between contracts - RM205 has an extra FB MODE
# column - so columns are located by header text. The defaults are the
# RM206 layout, used for a page whose table carries no header row.
MASTER_COLUMNS = {
    "sn": ("S/N",),
    "date": ("COMPLETED DATE",),
    "ref": ("DEFECT REFERENCE",),
    "pq": ("PQ / FSR / SOR ITEMS", "PQ/FSR/SOR ITEMS"),
    "length": ("LENGTH",),
    "width": ("WIDTH",),
    "qty": ("QTY",),
}
DEFAULT_COLUMNS = {"sn": 0, "date": 2, "ref": 4, "pq": 11, "length": 12, "width": 13, "qty": 15}


def header_match(row, wanted=MASTER_COLUMNS):
    """Map field -> column index for every wanted header this row carries."""
    cells = [" ".join((c or "").split()).upper() for c in row]
    cols = {}
    for field, names in wanted.items():
        for j, c in enumerate(cells):
            if c in names:
                cols[field] = j
                break
    return cols


def header_columns(row, wanted=MASTER_COLUMNS):
    """Map field -> column index when the row is the table header, else None."""
    cols = header_match(row, wanted)
    return cols if len(cols) == len(wanted) else None


def is_rm_master(pdf_bytes):
    """True when the sheet carries the RM205/RM206 table header."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:3]:
            for table in page.extract_tables():
                if any(header_columns(row) for row in table):
                    return True
    return False


def parse_master(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    records = {}
    cols = DEFAULT_COLUMNS
    # Columns are read from the header row. Falling back to fixed positions
    # for a whole sheet would read the wrong columns without saying so, so a
    # sheet whose header is never found is an error, not a silent guess.
    seen_header = False
    near_miss = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pno, p in enumerate(pdf.pages):
            if pno >= len(doc):
                break
            fp = doc[pno]

            for table in p.find_tables():
                rows = table.extract()
                if not rows or max(len(r) for r in rows if r) < 16:
                    continue

                current = None
                for i, raw in enumerate(rows):
                    found = header_match(raw)
                    if len(found) == len(MASTER_COLUMNS):
                        cols, seen_header = found, True
                        continue
                    if len(found) >= 3:  # a header row, but reworded
                        near_miss = sorted(set(MASTER_COLUMNS) - set(found))
                        continue

                    row = list(raw) + [None] * (max(cols.values()) + 1 - len(raw))
                    sn = (row[cols["sn"]] or "").strip()
                    pqm = PQ_RE.search((row[cols["pq"]] or "").replace("\n", " "))
                    if not pqm:
                        continue

                    if sn.isdigit():
                        cells = table.rows[i].cells
                        ref = norm_ref(clip_text(fp, cells[cols["ref"]] if len(cells) > cols["ref"] else None))
                        cdate = parse_date(clip_text(fp, cells[cols["date"]] if len(cells) > cols["date"] else None))
                        if not ref:
                            continue
                        current = ref
                        records[ref] = {"sn": int(sn), "date": cdate, "jobs": []}

                    if current:
                        records[current]["jobs"].append({
                            "pq": pqm.group(0).upper(),
                            "length": num(row[cols["length"]]),
                            "width": num(row[cols["width"]]),
                            "qty": num(row[cols["qty"]]),
                        })

    doc.close()
    if not seen_header:
        expected = ", ".join(names[0] for names in MASTER_COLUMNS.values())
        if near_miss:
            missing = ", ".join(MASTER_COLUMNS[f][0] for f in near_miss)
            raise ValueError(f"This mastersheet's table header is missing: {missing}. "
                             f"Expected a header row with: {expected}.")
        raise ValueError(f"No mastersheet table header found. "
                         f"Expected a header row with: {expected}.")
    return records
