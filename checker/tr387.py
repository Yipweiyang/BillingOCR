"""
TR387 (SP-717) batches: a mastersheet PDF with no defect references, and
one folder of site photos per S/N - "05-06. SERANGOON ROAD LP 71" holds
the photos for S/N 5 and 6. There is no report, no ITEM box and no OIC
page; the evidence is the photos' watermark dates and the dimensions
hand-written on the board held up in them.
"""
import io
import os
import re

import pdfplumber
from PIL import Image

from .common import PQ_RE, num, ocr_image, parse_date
from .mastersheet import header_columns
from .parallel import pmap
from .photos import board_dims, dims_match, parse_timestamp

TR387_COLUMNS = {
    "sn": ("S/N",),
    "location": ("LOCATION",),
    "landmark": ("LAND MARK",),
    "length": ("LENGTH",),
    "width": ("WIDTH",),
    "qty": ("QTY",),
    "pq": ("PQ/FSR/SOR ITEMS", "PQ / FSR / SOR ITEMS"),
    "date": ("COMPLETED DATE",),
}
FOLDER_RE = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?\.")
# "0.45x2": tiles, a size and how many - the board shows ".250x.450=2nos".
MULTIPLE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+)$")
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png")


# ---------- Mastersheet ----------
def dimension_values(cell):
    """
    The numbers stacked in one LENGTH or WIDTH cell as (value, sign, count):
    "1\\n0.6" is two areas, "2\\nLess\\n1" deducts the second, "0.45x2" is
    two tiles of 0.45, and labels such as "(Grating)" are skipped.
    """
    out, sign = [], 1
    for line in (cell or "").splitlines():
        t = line.strip()
        if t.lower() == "less":
            sign = -1
            continue
        m = MULTIPLE_RE.match(t)
        if m:
            out.append((float(m.group(1)), sign, int(m.group(2))))
        elif re.fullmatch(r"\d+(?:\.\d+)?", t):
            out.append((float(t), sign, 1))
    return out


def row_areas(length_cell, width_cell):
    """Pair the stacked lengths and widths into [{length, width, count, sign}], or [] if they do not pair up."""
    lengths, widths = dimension_values(length_cell), dimension_values(width_cell)
    if not lengths or len(lengths) != len(widths):
        return []
    return [{"length": L, "width": W, "count": lc * wc, "sign": s}
            for (L, s, lc), (W, _, wc) in zip(lengths, widths)]


def parse_tr387_master(pdf_bytes):
    """{S/N: {"sn", "ref", "date", "location", "landmark", "jobs": [{"pq", "qty"}], "areas"}}"""
    items = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                cols = None
                for row in table:
                    found = header_columns(row, TR387_COLUMNS)
                    if found:
                        cols = found
                        continue
                    if cols is None:
                        continue
                    row = list(row) + [None] * (max(cols.values()) + 1 - len(row))
                    sn = (row[cols["sn"]] or "").strip()
                    pqm = PQ_RE.search(row[cols["pq"]] or "")
                    item_code = pqm.group(0).upper() if pqm else (row[cols["pq"]] or "").strip().upper()
                    if not sn.isdigit() or not item_code:
                        continue
                    items[int(sn)] = {
                        "sn": int(sn),
                        "ref": "",
                        "date": parse_date(row[cols["date"]]),
                        "location": " ".join((row[cols["location"]] or "").split()),
                        "landmark": " ".join((row[cols["landmark"]] or "").split()),
                        "jobs": [{"pq": item_code, "length": None, "width": None, "qty": num(row[cols["qty"]])}],
                        "areas": row_areas(row[cols["length"]], row[cols["width"]]),
                    }
    return items


# ---------- Photo folders ----------
# The folder is named after the site and so is the mastersheet row, so the
# two can be checked against each other: matching by S/N alone cannot
# notice a lamp-post number that disagrees.
NO_SUFFIX_RE = re.compile(r"\(NO\.?\s*\d+\)")
FOLDER_PREFIX_RE = re.compile(r"^\s*\d+(?:\s*-\s*\d+)?\s*\.")
ABBREVIATIONS = (("JLN", "JALAN"), ("HSE", "HOUSE"), ("OPP", "OPPOSITE"))


def normalise_place(text):
    """ "05-06. SERANGOON ROAD LP 71" and "Serangoon Road (NO.1) LP 71" to one form."""
    t = NO_SUFFIX_RE.sub(" ", FOLDER_PREFIX_RE.sub(" ", str(text).upper()))
    for short, long in ABBREVIATIONS:
        t = t.replace(short, long)
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", t).split())


def location_note(item, folder_name):
    """How the folder name and the mastersheet row disagree about the site, or None."""
    master = normalise_place(f"{item['location']} {item['landmark']}")
    folder = normalise_place(folder_name)
    words = lambda place: {w for w in place.split() if not w.isdigit()}  # noqa: E731
    same_place = words(master) <= words(folder) or words(folder) <= words(master)
    if same_place and re.findall(r"\d+", master) == re.findall(r"\d+", folder):
        return None
    return f"folder '{folder_name}' vs mastersheet '{item['location']} {item['landmark']}'"


def folder_sns(name):
    """ "05-06. SERANGOON ROAD LP 71" -> [5, 6]; None for a folder not named by S/N."""
    m = FOLDER_RE.match(name)
    if not m:
        return None
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    return list(range(first, last + 1)) if last >= first else None


def photo_folders(batch_dir):
    """[(folder name, [S/N], [photo paths, date subfolders included])] for every S/N folder."""
    out = []
    for name in sorted(os.listdir(batch_dir)):
        path = os.path.join(batch_dir, name)
        sns = folder_sns(name) if os.path.isdir(path) else None
        if not sns:
            continue
        photos = sorted(
            os.path.join(root, f)
            for root, _, files in os.walk(path)
            for f in files
            if f.lower().endswith(PHOTO_EXTENSIONS)
        )
        out.append((name, sns, photos))
    return out


def read_photo(path, folder_dir):
    """OCR one site photo once; its text is reused by every check."""
    subdir, base = os.path.split(os.path.relpath(path, folder_dir))
    with Image.open(path) as im:
        text = ocr_image(im)
    dates = [d for d, source, _ in parse_timestamp(text) if source == "watermark"]
    return {
        "path": path,
        "label": f"{subdir}/{base[:8]}" if subdir else base[:8],
        # Some folders split their photos into date subfolders ("10-04-2026").
        "group": subdir or None,
        "text": text,
        "date": max(dates) if dates else None,
        "dims": board_dims(text),
    }


def photos_for(sn, sns, photos, items):
    """
    The photos in a folder that belong to one S/N. A folder shared by
    several S/Ns is split using the board: a photo whose dimensions match
    only another S/N's areas is theirs. Photos without readable
    dimensions stay with every S/N of the folder.
    """
    if len(sns) == 1:
        return photos

    def shows(photo, other):
        areas = items.get(other, {}).get("areas", [])
        return any(dims_match(a, d, loose=True) for a in areas for d in photo["dims"])

    return [p for p in photos
            if shows(p, sn) or not any(shows(p, other) for other in sns if other != sn)]


def last_day_photos(photos):
    """
    The photos taken on the latest watermark date of each photo group - the
    finished work, taken as the completion evidence - plus any photo whose
    date could not be read, so it is reported rather than silently dropped.
    """
    groups = {}
    for p in photos:
        groups.setdefault(p["group"], []).append(p)
    out = []
    for group in groups.values():
        dates = [p["date"] for p in group if p["date"]]
        last = max(dates) if dates else None
        out.extend(p for p in group if p["date"] is None or p["date"] == last)
    return out


def _read_one_photo(job):
    """One site photo, in a worker process."""
    path, folder_dir = job
    return read_photo(path, folder_dir)


def read_tr387_batch(batch_dir, master_bytes, progress=None):
    items = parse_tr387_master(master_bytes)
    folders = photo_folders(batch_dir)

    # Every photo of the batch is OCR'd once, in parallel - by far the
    # slowest part of a TR387 run.
    jobs = [(path, os.path.join(batch_dir, name)) for name, _, paths in folders for path in paths]
    by_path = {photo["path"]: photo for photo in pmap(_read_one_photo, jobs, progress=progress)}

    evidence = []
    for name, sns, paths in folders:
        photos = [by_path[p] for p in paths]
        for sn in sns:
            own = photos_for(sn, sns, photos, items)
            evidence.append({
                "key": sn,
                "source": name,
                "location_note": location_note(items[sn], name) if sn in items else None,
                "claimed_jobs": None,
                "sketch_jobs": None,
                "board_dims": [{**d, "label": p["label"]} for p in own for d in p["dims"]],
                # The raw OCR text too: a board too garbled to parse can still
                # confirm the dimension the mastersheet states.
                "board_texts": [{"label": p["label"], "text": p["text"], "path": p["path"]} for p in own],
                "after_photos": last_day_photos(own),
                "oic": None,
            })
    return items, evidence
