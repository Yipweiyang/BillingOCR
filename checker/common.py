"""Helpers shared by the extractors and checks: OCR, refs, numbers, dates."""
import io
import re
from datetime import datetime

import fitz
import numpy as np
from dateutil import parser as dtparser
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

from . import cache

NUM_TOL = 0.02
# Sector prefix varies by contract: NE4 (RM206), SW2 (RM205).
# Lookarounds rather than \b so a filename suffix such as "_R1" still matches.
REF_RE = re.compile(r"(?<![A-Z])([A-Z]{2})\s*(\d+)\s*[-–—]?\s*([WE])\s*[-–—]?\s*(\d{4,6})(?!\d)", re.I)
# The item code may end in a letter and digit, e.g. PQ30.3.1c1 (TR387).
PQ_RE = re.compile(r"\bPQ\d+(?:\.\d+)+(?:[A-Za-z]\d*)?\b", re.I)

_OCR = None
_OCR_OPTIONS = {}


def use_single_thread():
    """
    Build the OCR engine with one thread from here on.

    By default the engine spreads each image over every core, which is
    right for one process and disastrous for several: eight workers each
    claiming the whole machine thrash. One thread costs ~40% per image
    (0.92s -> 1.30s here) and lets eight run at once. onnxruntime takes
    this from its session options, not from OMP_NUM_THREADS.
    """
    global _OCR, _OCR_OPTIONS
    _OCR_OPTIONS = {"intra_op_num_threads": 1}
    _OCR = None


def ocr_engine():
    global _OCR
    if _OCR is None:
        _OCR = RapidOCR(**_OCR_OPTIONS)
    return _OCR


def ocr_image(img):
    """
    The text of one image, read once and then remembered - see cache.py.
    Every OCR in the project comes through here, so caching it covers site
    photos, scanned pages and the higher-resolution board re-reads alike.
    """
    rgb = img.convert("RGB")
    key = cache.key_for(rgb)
    remembered = cache.get(key)
    if remembered is not None:
        return remembered

    result, _ = ocr_engine()(np.array(rgb))
    text = "\n".join(str(x[1]) for x in result if len(x) >= 2 and x[1]) if result else ""
    cache.put(key, text)
    return text


def norm_ref(text):
    """
    Normalize a defect reference even when the PDF/filename contains
    spaces or different dash characters, e.g.:
        NE4-E-42446
        NE4-E- 42446
        NE4 - E - 42446
    """
    if not text:
        return None

    m = REF_RE.search(str(text).upper())
    if not m:
        return None

    return f"{m.group(1).upper()}{m.group(2)}-{m.group(3).upper()}-{m.group(4)}"


def ref_number(ref):
    """Return the numeric defect suffix, e.g. 43055."""
    if not ref:
        return None
    m = re.search(r"(\d{4,6})$", ref)
    return m.group(1) if m else None


def num(value):
    if value is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


def close(a, b, tol=NUM_TOL):
    return a is not None and b is not None and abs(a - b) <= tol


# A size in a sketch: "1.4m x 0.2m", "0.3m x 0.3m 5 Nos", with or without "= 0.28m2" after it.
# A triangle is written as half a rectangle: "1/2 x 2.3m x 1.0m".
SIZE_RE = re.compile(
    r"(?P<half>1\s*/\s*2\s*[xX×]\s*)?"
    r"(\d+(?:\.\d+)?)\s*m?\s*[xX×]\s*(\d+(?:\.\d+)?)\s*m?(?:\s*[xX×]?\s*(\d+)\s*Nos?\b\.?)?", re.I)


def visible_text(page):
    """
    The page's text without what an image drawn later covers. A report
    built from a copy of another keeps that one's QUANTITY lines in the text
    layer, hidden under the new map.
    """
    log = page.get_bboxlog()
    images = [(fitz.Rect(r), i) for i, (kind, r) in enumerate(log) if kind == "fill-image"]
    texts = [(fitz.Rect(r), i) for i, (kind, r) in enumerate(log) if kind == "fill-text"]

    def hidden(w):
        c = fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        drawn = max((i for r, i in texts if r.contains(c)), default=-1)
        return any(i > drawn and r.contains(c) for r, i in images)

    return " ".join(w[4] for w in page.get_text("words") if not hidden(w))


def sketch_text(page):
    """
    The text a reader sees on a sketch page. Some reports draw the QUANTITY
    lines in a font with no text mapping, so a page whose text layer gives
    no size is read by OCR instead. Every reading of the sketch goes
    through here, so the checks all see the same sketch.
    """
    text = visible_text(page)
    if not SIZE_RE.search(text):
        pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
        text = ocr_image(Image.open(io.BytesIO(pix.tobytes("png"))))
    return text


def sketch_sizes(page):
    """
    Every 'L x W' on a sketch page as [{"length", "width", "count", "half", "qty", "less"}].
    less marks a deduction - an area inside the repair that was not worked
    on, such as a grating or tactile tiles - written "Less grating area
    0.85m x 1m" or "2.2m x 2.1m - 1m x 0.85m". half marks a triangle.
    """
    text = sketch_text(page)
    out, end = [], 0
    for m in SIZE_RE.finditer(text):
        before = text[end:m.start()].strip()
        end = m.end()
        length, width, count, half = float(m.group(2)), float(m.group(3)), int(m.group(4) or 1), bool(m.group("half"))
        size = {"length": length, "width": width, "count": count, "half": half,
                "qty": length * width * count * (0.5 if half else 1),
                "less": bool(re.search(r"\bless\b", before, re.I)) or before in ("-", "–", "—")}
        if size not in out:
            out.append(size)
    return out


def sizes_sum(sizes):
    """Sketch sizes as the sum they stand for: "7.3 x 1.5 - 0.3 x 0.3 x 8 nos"."""
    out = ""
    for s in sizes:
        sign = " - " if s.get("less") else " + "
        size = f"{'1/2 x ' if s.get('half') else ''}{s['length']:g} x {s['width']:g}" \
            f"{' x %d nos' % s['count'] if s.get('count', 1) > 1 else ''}"
        out += (sign if out else sign.strip(" +")) + size
    return out


def sizes_net(sizes):
    """What the sketch's sizes come to, deductions taken off."""
    return sum(-s["qty"] if s.get("less") else s["qty"] for s in sizes)


def area_total(areas):
    """Net area of [{length, width, count, sign}], deductions (sign -1) taken off."""
    return sum(a["sign"] * a["length"] * a["width"] * a.get("count", 1) for a in areas)


def parse_date(text):
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dtparser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception:
        return None


def fmt_date(d):
    return d.strftime("%d-%m-%Y") if d else "Unreadable"


def clip_text(page, bbox):
    return page.get_text("text", clip=fitz.Rect(*bbox)).strip() if bbox else ""


def is_readable(text):
    """
    Some pages carry a long but corrupted text layer - the same broken
    font encoding that turns a defect ref into "NE4-(-43055" - so length
    alone is not evidence that the text can be understood. Real English
    pages are overwhelmingly plain ASCII.
    """
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < 80:
        return False
    return sum(c.isascii() and c.isalnum() for c in stripped) / len(stripped) >= 0.5


def page_text_with_ocr(page):
    native = page.get_text("text")
    if is_readable(native):
        return native
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    return native + "\n" + ocr_image(img)
