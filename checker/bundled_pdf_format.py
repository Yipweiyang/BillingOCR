"""
TR388 (CFM) batches: a one-page mastersheet listing every sector, and
bundle PDFs that hold a whole sector's incidents one after another.

Each incident in a bundle is three pages, always in this order:

    1. the OIC's instruction - a WhatsApp screenshot, image only
    2. MEASUREMENT & SKETCH - ref, location, Item/Qty/Unit box, "Area" lines
    3. Jobs Complete Record Sheet - photos labelled Before/During/After

The sketch page is the anchor: it has a text layer, so an incident is
found without OCR and the pages either side of it are its OIC and photos.

A defect number (D.No) can repeat on the mastersheet - 15612W is three
lamp posts - so a repeated number is told apart by its landmark ("Lp 46").
"""
import io
import re

import fitz
import pdfplumber
from PIL import Image

from .common import close, num, ocr_image, parse_date
from .mastersheet import header_columns, header_match
from .parallel import pmap
from .photos import native_image, parse_timestamp, photo_distances
from .photo_folder_format import normalise_place

TR388_COLUMNS = {
    "sn": ("S.NO",),
    "start": ("STARTING DATE",),
    "date": ("DATE & TIME OF COMPLETION",),
    "sector": ("SECTOR",),
    "officer": ("OFFICER INCHARGE",),
    "dno": ("D.NO (CHC REF)",),
    "location": ("LOCATION/ROAD NAME",),
    "landmark": ("LAND MARK",),
    "pq": ("PQ / SOR / FSR ITEM",),
    "qty": ("QUANTITY",),
    "unit": ("UNIT",),
}
# What each line is billed at, for the price check; optional like the RM ones.
TR388_PRICE_COLUMNS = {
    "rate": ("UNIT RATE (PQ)",),
    "amount": ("TOTAL COST (PQ)",),
}
# "15612W-#13639", "NW2-15970W", "16097E #13957": the D.No is the number
# directly followed by E or W.
DNO_RE = re.compile(r"(?<!\d)(\d{3,6})\s*([EW])(?![A-Z])", re.I)
PQ_CODE_RE = re.compile(r"PQ\s*(\d+(?:\.\d+)+[A-Za-z]?\d*)", re.I)
SECTOR_RE = re.compile(r"\b([NSEW]{1,2}\d)\b")

SKETCH_MARK = "MEASUREMENT & SKETCH"
PHOTO_MARK = "Jobs Complete Record Sheet"

# Sketch arithmetic: "1.5m × 1.2m = 1.8m2", "0.3m x 0.3m 5 Nos = 0.45m2"
# and deductions such as "6.75m2 - 0.85m2 = 5.9m2".
NUMBER = r"(\d+(?:\.\d+)?)"
PRODUCT_RE = re.compile(
    rf"{NUMBER}\s*m?\s*[xX×]\s*{NUMBER}\s*m?\s*(?:{NUMBER}\s*Nos?\.?)?\s*=\s*{NUMBER}", re.I)
DIFFERENCE_RE = re.compile(rf"{NUMBER}\s*(?:m2|m²|m)?\s*-\s*{NUMBER}\s*(?:m2|m²|m)?\s*=\s*{NUMBER}", re.I)


def dno(text):
    """The D.No in any spelling of a reference, e.g. '15612W', or None."""
    m = DNO_RE.search(str(text or "").upper())
    return f"{m.group(1)}{m.group(2)}" if m else None


def pq_code(text):
    m = PQ_CODE_RE.search(str(text or ""))
    return f"PQ{m.group(1)}".upper() if m else None


def landmark_key(text):
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).split())


def item_key(number, landmark, repeated):
    """The mastersheet's D.No, plus the landmark only when the number repeats."""
    return f"{number} Lp {landmark}" if number in repeated else number


# ---------- Mastersheet ----------
def is_tr388_master(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for table in pdf.pages[0].extract_tables():
            if any(header_columns(row, TR388_COLUMNS) for row in table):
                return True
    return False


def parse_tr388_master(pdf_bytes):
    """
    {key: {"sn", "ref", "date", "start", "sector", "officer", "location",
    "landmark", "jobs": [{"pq", "length", "width", "qty", "unit"}]}}.
    A row with no S.No carries another job for the row above it.
    """
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                cols = None
                for raw in table:
                    found = header_columns(raw, TR388_COLUMNS)
                    if found:
                        cols = {**found, **header_match(raw, TR388_PRICE_COLUMNS)}
                        continue
                    if cols is None:
                        continue
                    row = list(raw) + [None] * (max(cols.values()) + 1 - len(raw))
                    cell = lambda f: " ".join((row[cols[f]] or "").split()) if f in cols else ""  # noqa: E731
                    code = pq_code(cell("pq"))
                    if cell("sn").isdigit():
                        number = dno(cell("dno"))
                        if not number:
                            continue
                        rows.append({
                            "sn": int(cell("sn")),
                            "ref": number,
                            "date": parse_date(cell("date")),
                            "start": parse_date(cell("start")),
                            "sector": (SECTOR_RE.findall(cell("sector").upper()) or [cell("sector")])[-1],
                            "officer": cell("officer"),
                            "location": cell("location"),
                            "landmark": cell("landmark"),
                            "jobs": [],
                        })
                    if code and rows:
                        rows[-1]["jobs"].append({"pq": code, "length": None, "width": None,
                                                 "qty": num(cell("qty")), "unit": cell("unit") or None,
                                                 "rate": num(cell("rate")), "amount": num(cell("amount"))})

    counts = {}
    for r in rows:
        counts[r["ref"]] = counts.get(r["ref"], 0) + 1
    repeated = {n for n, c in counts.items() if c > 1}
    return {item_key(r["ref"], landmark_key(r["landmark"]), repeated): r for r in rows}


# ---------- Bundle ----------
def is_tr388_bundle(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return any(SKETCH_MARK in page.get_text() for page in doc) and \
            any(PHOTO_MARK in page.get_text() for page in doc)
    finally:
        doc.close()


def word_lines(words, tolerance=4):
    """Words grouped into visual lines, top to bottom, each as its text."""
    lines = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        yc = (w[1] + w[3]) / 2
        for ln in lines:
            if abs(ln["y"] - yc) <= tolerance:
                ln["words"].append(w)
                break
        else:
            lines.append({"y": yc, "words": [w]})
    return [" ".join(w[4] for w in sorted(ln["words"], key=lambda w: w[0]))
            for ln in sorted(lines, key=lambda x: x["y"])]


def labelled_value(words, label):
    """The text right of 'label :' on the sketch page's header lines."""
    for text in word_lines(words):
        m = re.search(rf"{re.escape(label)}\s*:\s*(.+?)(?:\s+(?:Defect Reference No|Date Commenced|Date Completed)\b|$)",
                      text)
        if m:
            return m.group(1).strip(" :")
    return None


def item_box_jobs(page):
    """
    The Item No. / Qty / Unit box: [{"pq", "qty", "unit"}] in line order,
    or None if the box is not on the page. Words are grouped into lines by
    position, because the text layer does not keep the table's order.
    """
    words = page.get_text("words")
    header = {w[4].upper().rstrip("."): w for w in words if w[4].upper().rstrip(".") in ("ITEM", "QTY", "UNIT")}
    if len(header) < 3:
        return None
    top = max(w[3] for w in header.values())
    left, right = header["ITEM"][0] - 40, header["UNIT"][2] + 30

    below = [w for w in words if (w[1] + w[3]) / 2 > top and left <= (w[0] + w[2]) / 2 <= right]

    jobs = []
    for text in word_lines(below):
        m = re.match(r"(PQ\s*\d+(?:\.\d+)+[A-Za-z]?\d*)\s+(\d+(?:\.\d+)?)\s*(\S+)?", text, re.I)
        if m:
            jobs.append({"pq": pq_code(m.group(1)), "qty": float(m.group(2)), "unit": m.group(3)})
    return jobs


def sketch_errors(page):
    """Arithmetic in the sketch's 'Area' lines that does not add up."""
    errors = []
    lines = dict.fromkeys(" ".join(x.split()) for x in page.get_text().splitlines() if "=" in x)
    for line in lines:
        for m in PRODUCT_RE.finditer(line):
            actual = float(m.group(1)) * float(m.group(2)) * (int(float(m.group(3))) if m.group(3) else 1)
            if not close(actual, float(m.group(4))):
                errors.append(f'"{line}" should be {actual:.2f}')
        for m in DIFFERENCE_RE.finditer(line):
            actual = float(m.group(1)) - float(m.group(2))
            if not close(actual, float(m.group(3))):
                errors.append(f'"{line}" should be {actual:.2f}')
    return errors


def sketch_sizes(page):
    """Every 'L x W =' in the sketch's Area lines, deductions included, as [{"length", "width"}]."""
    out = []
    for m in PRODUCT_RE.finditer(" ".join(page.get_text().split())):
        size = {"length": float(m.group(1)), "width": float(m.group(2))}
        if size not in out:
            out.append(size)
    return out


def after_photo_images(doc, page):
    """
    The images labelled After. On this template the label sits above its
    picture, so each one takes the nearest image below that lies under it.
    """
    placed = [(info[0], rect) for info in page.get_images(full=True) for rect in page.get_image_rects(info[0])]
    out = []
    for w in page.get_text("words"):
        if w[4].strip().upper() != "AFTER":
            continue
        xc = (w[0] + w[2]) / 2
        below = [(xref, r) for xref, r in placed if r.x0 <= xc <= r.x1 and r.y0 >= w[3] - 5]
        if below:
            out.append(min(below, key=lambda x: x[1].y0))
    return out


def split_incidents(doc):
    """[(OIC page index or None, sketch page index, photo page index or None)]."""
    kinds = []
    for page in doc:
        text = page.get_text()
        kinds.append("sketch" if SKETCH_MARK in text else "photos" if PHOTO_MARK in text
                     else "blank" if not text.strip() else "other")

    out = []
    for i, kind in enumerate(kinds):
        if kind != "sketch":
            continue
        # The screenshot is image only; anything with text is another template.
        oic = i - 1 if i > 0 and kinds[i - 1] == "blank" and doc[i - 1].get_images() else None
        photos = i + 1 if i + 1 < len(kinds) and kinds[i + 1] == "photos" else None
        out.append((oic, i, photos))
    return out


def read_incident(doc, source, oic, sketch, photos):
    """Everything but the OCR of one incident; the After photos come back as images to read."""
    page = doc[sketch]
    words = page.get_text("words")
    ref = labelled_value(words, "Defect Reference No.") or ""
    location = labelled_value(words, "Location") or ""
    lp = re.search(r"\bLp\s+(.+)$", location, re.I)

    pages = [p + 1 for p in (oic, sketch, photos) if p is not None]
    record = {
        "ref_text": ref,
        "dno": dno(ref),
        "location": location,
        "landmark": landmark_key(lp.group(1)) if lp else "",
        "source": f"{source} p{pages[0]}-{pages[-1]}",
        "claimed_jobs": item_box_jobs(page),
        "sketch_errors": sketch_errors(page),
        "sketch_sizes": sketch_sizes(page),
        "oic": ({"found": True, "detail": f"OIC instruction screenshot on page {oic + 1}"} if oic is not None
                else {"found": False, "detail": f"No OIC instruction page before the sketch on page {sketch + 1}"}),
        "photos": [],
        "photo_dims": photo_distances(doc[photos], photos) if photos is not None else [],
    }
    if photos is not None:
        for n, (xref, rect) in enumerate(after_photo_images(doc, doc[photos]), 1):
            record["photos"].append({
                "label": f"p{photos + 1} After {n}",
                "image": native_image(doc, xref),
                # The watermark can sit at the picture's edge, where the
                # page crop may differ from the embedded image; the
                # rendered picture is the fallback read.
                "render": doc[photos].get_pixmap(clip=rect, dpi=200).tobytes("png"),
            })
    return record


def watermark_crop(image, scale=2):
    """The bottom-right corner where the camera stamps its date, enlarged."""
    w, h = image.size
    corner = image.crop((int(w * 0.4), int(h * 0.75), w, h))
    return corner.resize((corner.width * scale, corner.height * scale), Image.LANCZOS)


def _read_after_photo(photo):
    """
    OCR one After photo, in a worker process. The watermark is small, and
    a single read can drop a digit ("17 Apr" read as "7 Apr"), so the
    enlarged corner is always read as well; a date seen in either read counts.
    """
    text = ocr_image(photo["image"]) + "\n" + ocr_image(watermark_crop(photo["image"]))
    if not any(source == "watermark" for _, source, _ in parse_timestamp(text)):
        text += "\n" + ocr_image(Image.open(io.BytesIO(photo["render"])))
    return {"label": photo["label"], "image": photo["image"], "text": text}


def match_key(record, repeated):
    number = record["dno"]
    if number in repeated:
        return item_key(number, record["landmark"], repeated)
    return number


def location_note(item, record):
    master = set(normalise_place(item["location"]).split())
    report = set(normalise_place(record["location"]).split())
    return None if master <= report else f"report '{record['location']}' vs mastersheet '{item['location']}'"


def read_tr388_batch(master_bytes, bundles, progress=None):
    """bundles is [(filename, bytes)] of the incident bundles."""
    items = parse_tr388_master(master_bytes)
    repeated = {m["ref"] for k, m in items.items() if k != m["ref"]}

    records = []
    for filename, data in bundles:
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            for oic, sketch, photos in split_incidents(doc):
                records.append(read_incident(doc, filename, oic, sketch, photos))
        finally:
            doc.close()

    flat = [p for r in records for p in r["photos"]]
    read = iter(pmap(_read_after_photo, flat, progress=progress))

    evidence = []
    for r in records:
        key = match_key(r, repeated) if r["dno"] else None
        evidence.append({
            "key": key,
            "source": r["source"],
            "location_note": location_note(items[key], r) if key in items else None,
            "claimed_jobs": r["claimed_jobs"],
            "sketch_jobs": None,
            "sketch_errors": r["sketch_errors"],
            "sketch_sizes": r["sketch_sizes"],
            "after_photos": [next(read) for _ in r["photos"]],
            "photo_dims": r["photo_dims"],
            "oic": r["oic"],
        })
    return items, evidence
