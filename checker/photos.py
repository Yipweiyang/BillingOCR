"""Site photos: AFTER images, timestamps, app screenshots, hand-written board dimensions."""
import calendar
import io
import re
from datetime import date

import fitz
from PIL import Image

from .common import close, ocr_image

# OCR regularly reads the watermark's "Oct" as "0ct".
MONTHS = r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Sep|[O0]ct|Nov|Dec"
MONTH_NUM = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTH_NUM.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
MONTH_NUM["sept"] = 9

# The GPS-camera watermark burned into the site photos reads
# "Jan 2, 2026 6:20:24 PM", but OCR routinely drops every space and
# confuses '.' with ',', producing "Jan2.20266:20:24PM". Separators are
# therefore optional throughout, and because the time runs straight into
# the year the year is matched as exactly four digits with no trailing
# boundary assertion.
TS_MONTH_FIRST = re.compile(
    rf"(?P<mon>{MONTHS})[a-z]*\s*[.,]?\s*(?P<day>\d{{1,2}})\s*[.,-]?\s*(?P<year>20\d{{2}})", re.I)
TS_DAY_FIRST = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})\s*[.,-]?\s*(?P<mon>{MONTHS})[a-z]*\s*[.,-]?\s*(?:at\s*)?(?P<year>20\d{{2}})", re.I)
# Some camera apps stamp a numeric day-first date followed by a time,
# e.g. "9/10/25 15:30" (RM205). The trailing time is what separates it
# from a hand-written placard, so it counts as a watermark.
TS_NUMERIC_STAMP = re.compile(
    r"(?<!\d)(?P<d>\d{1,2})\s*/\s*(?P<m>\d{1,2})\s*/\s*(?P<y>20\d{2}|\d{2})\s*\d{1,2}\s*:\s*\d{2}")
# The paper placard held up in the photo, e.g. "DATE/TIME:02/01/2026".
TS_PLACARD = re.compile(r"(?<!\d)(?P<d>\d{1,2})\s*[/-]\s*(?P<m>\d{1,2})\s*[/-]\s*(?P<y>20\d{2}|\d{2})(?!\d)")

# Phone screenshots of the EFMS work-order form are sometimes tagged
# AFTER. They are app captures rather than site evidence, and any date
# they carry belongs to the screenshot, not to the repair.
APP_MARKERS = (
    "report (optional)", "report(optional)", "element type",
    "defect type", "defect cause", "proposed repair", "feedback description",
    "actions taken", "measurement unit", "inprg", "wear and tear",
)
# Any one of these is conclusive on its own, which matters because a badly
# OCR'd screenshot may surface only a single recognisable phrase.
APP_STRONG = (
    "review report", "wo attended", "work order submitted",
    "workordersubmitted", "submitted for review",
)
# The EFMS work order number, e.g. CFS/26/51719800 (the S is often read
# as a 5). It appears on screenshots only, never on a site photo.
WORK_ORDER_RE = re.compile(r"C[F=][S5]?\s*/\s*\d{2}\s*/\s*\d{6,}", re.I)

# Scales used for the second look at the on-site completion board. The
# board is hand-written, so OCR is unreliable at any single resolution -
# it reads at one scale and not another - and re-reading is only worth
# the cost when the camera watermark has already raised a doubt.
BOARD_SCALES = (2, 3)

# Dimensions hand-written on the board, e.g. "1.5×2.9m", "900×1.2m" (mm)
# or "600×.700" - a board number may have no leading digit. OCR reads the
# decimal point as ':' or '-' ("1:3×1.4"), so those are restored first, one
# line at a time so a watermark time is never joined to a dimension.
NUMBER = r"\d*\.?\d+"
BOARD_DECIMAL_RE = re.compile(r"(?<=\d)[:\-,·](?=\d)")
BOARD_DIM_RE = re.compile(rf"(?<![\d.])({NUMBER})\s*[xX×*]\s*({NUMBER})(?![\d.])")


def _make_date(year, month, day):
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def parse_timestamp(text):
    """
    Every plausible date in the OCR text of one photo, as
    (date, source, raw) with source 'watermark' or 'placard'.

    The camera watermark is the only trustworthy evidence of when a photo
    was taken; the placard is hand-written and frequently misread, so it
    is kept only as corroboration and never used to fail a report.
    """
    cleaned = " ".join(str(text).split())
    out = []

    def add(d, source, raw):
        if d and not any(d == x[0] for x in out):
            out.append((d, source, raw))

    def month(m):
        return MONTH_NUM.get(m.group("mon").lower().replace("0", "o"))

    for m in TS_DAY_FIRST.finditer(cleaned):
        add(_make_date(m.group("year"), month(m), m.group("day")), "watermark", m.group(0))
    for m in TS_MONTH_FIRST.finditer(cleaned):
        add(_make_date(m.group("year"), month(m), m.group("day")), "watermark", m.group(0))
    # Numeric stamps first: add() keeps the first source seen for a date,
    # so the placard pass below cannot demote them.
    for m in TS_NUMERIC_STAMP.finditer(cleaned):
        year = int(m.group("y"))
        add(_make_date(year + 2000 if year < 100 else year, m.group("m"), m.group("d")),
            "watermark", m.group(0))
    for m in TS_PLACARD.finditer(cleaned):
        year = int(m.group("y"))
        add(_make_date(year + 2000 if year < 100 else year, m.group("m"), m.group("d")),
            "placard", m.group(0))
    return out


def is_app_screenshot(text):
    t = " ".join(str(text).lower().split())
    if WORK_ORDER_RE.search(t) or any(marker in t for marker in APP_STRONG):
        return True
    return sum(marker in t for marker in APP_MARKERS) >= 2


def after_images(page):
    labels = [w for w in page.get_text("words") if w[4].strip().upper() == "AFTER"]
    if not labels:
        return []

    images = []
    for info in page.get_images(full=True):
        xref = info[0]
        for rect in page.get_image_rects(xref):
            images.append((xref, rect))

    out, seen = [], set()
    for w in labels:
        c = fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        matches = [(xref, r) for xref, r in images if r.contains(c)]
        if matches:
            xref, r = min(matches, key=lambda x: x[1].get_area())
            if xref not in seen:
                seen.add(xref)
                out.append(xref)
    return out


def native_image(doc, xref):
    info = doc.extract_image(xref)
    return Image.open(io.BytesIO(info["image"])).convert("RGB")


def load_photo(photo):
    """
    The image of a photo record. Folder batches keep only the path, so a
    few hundred full-size site photos are never held in memory at once.
    """
    if photo.get("image") is not None:
        return photo["image"]
    with Image.open(photo["path"]) as im:
        return im.convert("RGB")


def board_confirms(image, master_date):
    """
    Re-read the white completion board held up in the photo at higher
    resolution, looking for the mastersheet date. The board records when
    the work was signed off on site, which can legitimately differ from
    when the photo was taken, so it can clear a doubt but never raise
    one: a misread board only ever produces a date nobody acts on.
    """
    for scale in BOARD_SCALES:
        bigger = image.resize((image.width * scale, image.height * scale), Image.LANCZOS)
        for d, source, _ in parse_timestamp(ocr_image(bigger)):
            if source == "placard" and d == master_date:
                return True
    return False


# Tiles are written as a size and a count: "300×300=8nos", ".250x.450:2nos",
# "200x200-5nos". Read before decimal points are restored, which would
# otherwise turn "200-5" into "200.5".
BOARD_TILES_RE = re.compile(
    r"(?<![\d.])(\d*\.?\d+)\s*[xX×*]\s*(\d*\.?\d+)\s*[=:\-#二]\s*(\d{1,3})\s*n[o0]", re.I)


def _metres(value):
    value = float(value)
    return value / 1000 if value >= 100 else value


def _pairs(line, pattern):
    """The "number x number" matches of one line, in metres, implausible sizes dropped."""
    for m in pattern.finditer(line):
        length, width = _metres(m.group(1)), _metres(m.group(2))
        if 0 < length <= 100 and 0 < width <= 100:
            yield length, width, m


def board_dims(text):
    """
    Every "L x W" hand-written on the board in the OCR text of one photo,
    in metres, as [{"length", "width", "count"}]. Values of 100 or more
    are millimetres ("900×1.2m" is 0.9 x 1.2); count is the number of
    tiles when the board gives one, else 1.
    """
    out = []

    def add(length, width, count=1):
        reading = {"length": length, "width": width, "count": int(count)}
        if reading not in out:
            out.append(reading)

    for raw in str(text).splitlines():
        for length, width, m in _pairs(raw, BOARD_TILES_RE):
            add(length, width, m.group(3))
        line = BOARD_DECIMAL_RE.sub(".", BOARD_TILES_RE.sub(" ", raw))
        for length, width, _ in _pairs(line, BOARD_DIM_RE):
            add(length, width)
    return out


def _digits(value):
    return f"{value:g}".replace(".", "").lstrip("0")


def dims_match(area, reading, loose=False):
    """
    Whether a board reading is this area, either way round. loose also
    accepts the same digits with the decimal point lost ("1x17" for 1 x 1.7),
    the commonest OCR slip on the boards.
    """
    def same(a, b):
        return close(a, b, tol=0.01) or (loose and _digits(a) == _digits(b))

    L, W = area["length"], area["width"]
    a, b = reading["length"], reading["width"]
    return (same(L, a) and same(W, b)) or (same(L, b) and same(W, a))


# Characters OCR returns instead of digits when reading marker on a board:
# "boox.6o0" is 600x.600, "-(00x9m" is 100x9m, "1+x18m" is 14x1.8m. Too
# unreliable to read a dimension with, but enough to confirm one the
# mastersheet already states.
BOARD_CONFUSIONS = str.maketrans({
    "o": "0", "O": "0", "b": "6", "l": "1", "I": "1", "i": "1", "|": "1",
    "s": "5", "S": "5", "g": "9", "z": "2", "Z": "2", "B": "8", "D": "0",
    "(": "1", ")": "1",
})
# As BOARD_DIM_RE, but accepting the characters OCR puts in place of the
# "x" as well, and without the lookarounds: by this point the line has
# already been rewritten, so its edges prove nothing.
BOARD_PAIR_RE = re.compile(rf"({NUMBER})\s*[xX×*y#%wt]\s*({NUMBER})")


def board_pairs(text):
    """Every "number x number" on a line, in metres, after undoing those confusions."""
    out = []
    for raw in str(text).splitlines():
        line = raw.translate(BOARD_CONFUSIONS)
        for variant in [line] + ([line.replace("+", "4")] if "+" in line else []):
            for length, width, _ in _pairs(variant, BOARD_PAIR_RE):
                out.append((length, width, raw.strip()))
    return out


def board_confirms_area(text, area):
    """
    The board line that could be this area, either way round, or None.

    This only ever confirms a dimension the mastersheet already states; it
    never produces one, because hand-writing OCR is not reliable enough to
    be believed on its own.
    """
    for a, b, raw in board_pairs(text):
        if dims_match(area, {"length": a, "width": b}, loose=True):
            return raw
    return None


# Site photos are large, so one extra pass is enough; the dimensions on a
# board that is still unreadable at 2x are gone for good.
BOARD_RESCAN_SCALES = (2,)


def board_area_rescan(photo, area, scales=BOARD_RESCAN_SCALES):
    """
    Re-read one photo's board at higher resolution, looking only for this
    area. Hand-writing OCR is scale-sensitive - "·600x1m" comes back as
    "W1X009e" at native size and "b00x1m" at 2x - so this is worth the
    cost once the ordinary reading has failed.
    """
    image = load_photo(photo)
    for scale in scales:
        bigger = image.resize((image.width * scale, image.height * scale), Image.LANCZOS)
        line = board_confirms_area(ocr_image(bigger), area)
        if line:
            return line
    return None
