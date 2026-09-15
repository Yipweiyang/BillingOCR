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


def header_columns(row):
    """Map field -> column index when the row is the table header, else None."""
    cells = [" ".join((c or "").split()).upper() for c in row]
    cols = {}
    for field, names in MASTER_COLUMNS.items():
        for j, c in enumerate(cells):
            if c in names:
                cols[field] = j
                break
    return cols if len(cols) == len(MASTER_COLUMNS) else None


def parse_master(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    records = {}
    cols = DEFAULT_COLUMNS

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
                    found = header_columns(raw)
                    if found:
                        cols = found
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
    return records
