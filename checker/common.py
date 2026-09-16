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
