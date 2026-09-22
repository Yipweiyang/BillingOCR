"""Reads an RM205/RM206 incident report PDF into an evidence record."""
import re
from itertools import combinations

import fitz

from .common import PQ_RE, close, norm_ref, ocr_image, page_text_with_ocr, ref_number
from .photos import after_images, native_image, photo_distances

MEAS_RE = re.compile(
    r"(?P<L>\d+(?:\.\d+)?)\s*m?\s*[xX×]\s*"
    r"(?P<W>\d+(?:\.\d+)?)\s*m?\s*=\s*"
    r"(?P<Q>\d+(?:\.\d+)?)\s*m(?:2|²)?",
    re.I,
)
MEAS_LESS_RE = re.compile(
    r"(?P<L>\d+(?:\.\d+)?)\s*m?\s*[xX×]\s*(?P<W>\d+(?:\.\d+)?)\s*m?\s*[-–—]\s*"
    r"\d+(?:\.\d+)?\s*m?\s*[xX×]\s*\d+(?:\.\d+)?\s*m?\s*=\s*"
    r"(?P<Q>\d+(?:\.\d+)?)\s*m(?:2|²)?",
    re.I,
)


def report_ref(doc, filename, expected_refs=None):
    """
    Resolve the incident report Defect Reference robustly.

    Priority:
    1. Valid reference found in page-1 PDF text.
    2. Valid reference found in the uploaded filename.
    3. Master-aware fallback: if the PDF text layer is corrupted
       (for example NE4-(-43055), match the unique numeric defect
       suffix against the mastersheet.

    This avoids false 'missing' flags caused by broken PDF text layers.
    """
    expected_refs = set(expected_refs or [])
    page_text = doc[0].get_text("text")

    # Prefer the document content when it resolves to a mastersheet ref.
    text_ref = norm_ref(page_text)
    if text_ref and (not expected_refs or text_ref in expected_refs):
        return text_ref

    # Filename fallback, allowing spaces around separators.
    filename_ref = norm_ref(filename)
    if filename_ref and (not expected_refs or filename_ref in expected_refs):
        return filename_ref

    # If we found a syntactically valid reference but it is not in the
    # mastersheet, keep it so the UI can show it as an extra upload.
    if text_ref:
        return text_ref
    if filename_ref:
        return filename_ref

    # Last-resort master-aware recovery. Some PDFs have corrupted text
    # such as "NE4-(-43055" where only the numeric suffix is reliable.
    if expected_refs:
        combined = f"{page_text} {filename}"
        numbers = set(re.findall(r"(?<!\d)(\d{4,6})(?!\d)", combined))

        matches = [
            ref for ref in expected_refs
            if ref_number(ref) in numbers
        ]

        if len(matches) == 1:
            return matches[0]

    return None


def measurements(page):
    text = page.get_text("text").replace("\n", " ")
    out = []
    # "2.2m x 2.1m - 1m x 0.85m = 3.77m2": the gross dimensions with the
    # deduction already taken off. Taken first and blanked out so MEAS_RE
    # does not read the deducted "1m x 0.85m" as its own job.
    for m in MEAS_LESS_RE.finditer(text):
        out.append({"length": float(m.group("L")), "width": float(m.group("W")), "qty": float(m.group("Q"))})
    text = MEAS_LESS_RE.sub(" ", text)
    for m in MEAS_RE.finditer(text):
        item = {"length": float(m.group("L")), "width": float(m.group("W")), "qty": float(m.group("Q"))}
        if item not in out:
            out.append(item)
    return out


def pq_candidates(page):
    words = page.get_text("words")
    H, W = page.rect.height, page.rect.width
    pqs, nums = [], []

    for w in words:
        t = w[4].strip()
        yc = (w[1] + w[3]) / 2
        if yc < 0.50 * H or w[0] > 0.45 * W:
            continue
        if PQ_RE.fullmatch(t):
            pqs.append(w)
        elif re.fullmatch(r"\d+(?:\.\d+)?", t):
            nums.append(w)

    out = []
    for pw in pqs:
        py = (pw[1] + pw[3]) / 2
        choices = []
        for nw in nums:
            ny = (nw[1] + nw[3]) / 2
            if nw[0] > pw[2] - 2 and abs(ny - py) <= 8:
                choices.append((abs(ny - py) + .01 * (nw[0] - pw[2]), nw))
        if choices:
            nw = min(choices, key=lambda x: x[0])[1]
            out.append({"pq": pw[4].upper(), "qty": float(nw[4])})
    return out


def relevant_measurements(ms, pcs):
    if not ms or not pcs:
        return ms
    selected = set()
    for target in [x["qty"] for x in pcs]:
        for size in range(1, len(ms) + 1):
            found = None
            for combo in combinations(range(len(ms)), size):
                if close(sum(ms[i]["qty"] for i in combo), target):
                    found = combo
                    break
            if found is not None:
                selected.update(found)
                break
    return [m for i, m in enumerate(ms) if i in selected] if selected else ms


def attach_pq(ms, pcs):
    jobs = [{**m, "pq": None} for m in ms]
    used = set()

    for i, m in enumerate(ms):
        options = [(j, p) for j, p in enumerate(pcs) if j not in used and close(p["qty"], m["qty"])]
        if options:
            j, p = options[0]
            used.add(j)
            jobs[i]["pq"] = p["pq"]

    if all(x["pq"] for x in jobs):
        return jobs

    unique = sorted(set(p["pq"] for p in pcs))
    if len(unique) == 1:
        return [{**m, "pq": unique[0]} for m in ms]

    individual = [p for p in pcs if any(close(p["qty"], m["qty"]) for m in ms)]
    unique = sorted(set(p["pq"] for p in individual))
    if len(unique) == 1:
        return [{**m, "pq": unique[0]} for m in ms]

    return jobs


def item_box_jobs(page):
    """
    The ITEM / QTY / UNIT box at the foot of page 1 - the quantities the
    contractor is actually claiming. This is a filled template: the
    original row is covered by a white rectangle and the real values are
    painted over it, so a line can carry two PQs and two quantities.
    PDF content order is paint order, so the last-drawn word on a line is
    the one a reader actually sees.

    Returns None when the box cannot be located at all.
    """
    words = page.get_text("words")

    header = {}
    for w in words:
        t = w[4].strip().upper()
        if t in ("ITEM", "QTY", "UNIT") and t not in header:
            header[t] = w
    if len(header) < 3:
        return None

    top = header["ITEM"][3]
    item_x = (header["ITEM"][0] - 30, header["ITEM"][2] + 60)
    qty_x = (header["QTY"][0] - 30, header["QTY"][2] + 30)

    lines = []

    def line_at(yc):
        for ln in lines:
            if abs(ln["y"] - yc) <= 6:
                return ln
        lines.append({"y": yc, "pq": [], "qty": []})
        return lines[-1]

    for order, w in enumerate(words):
        t = w[4].strip()
        xc, yc = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if yc < top:
            continue
        if PQ_RE.fullmatch(t) and item_x[0] <= xc <= item_x[1]:
            line_at(yc)["pq"].append((order, t.upper()))
        elif re.fullmatch(r"\d+(?:\.\d+)?", t) and qty_x[0] <= xc <= qty_x[1]:
            line_at(yc)["qty"].append((order, float(t)))

    return [
        {"pq": max(ln["pq"])[1], "qty": max(ln["qty"])[1]}
        for ln in sorted(lines, key=lambda x: x["y"])
        if ln["pq"] and ln["qty"]
    ]


def after_photos(doc):
    """Every image labelled AFTER, in page order."""
    photos = []
    for pno, page in enumerate(doc):
        for xref in after_images(page):
            image = native_image(doc, xref)
            # OCR'd here, while this report has a worker process to itself.
            photos.append({"label": f"p{pno + 1}", "image": image, "text": ocr_image(image)})
    return photos


def looks_like_oic_instruction(text):
    t = " ".join(text.lower().split())
    if len(re.sub(r"[^a-z0-9]", "", t)) < 25:
        return False

    request = any(x in t for x in ("please", "kindly", "assist", "proceed", "request", "instruct"))
    action = any(x in t for x in ("rectify", "repair", "fix", "render", "replace", "reinstate", "patch", "attend"))
    if request and action:
        return True

    # Recognises the five different supporting OIC formats supplied.
    if "defect record sheet" in t:
        return True
    # RM205 defect record: "04-09-2025 SW2-W-14225 / Location: ... / Remarks: ...".
    # Photo pages carry the ref and Location too, but never Remarks.
    if norm_ref(t) and "location" in t and "remarks" in t:
        return True
    markers = ("remarks", "location", "defect reference", "sector", "efms", "rm206", "footpath", "instructions")
    return sum(x in t for x in markers) >= 3


PHOTO_LABELS = {"BEFORE", "DURING", "AFTER"}


def photo_page(page):
    """A page of the photo template, which always labels its pictures."""
    return bool({w[4].strip().upper() for w in page.get_text("words")} & PHOTO_LABELS)


def oic_record(doc, own_ref=None):
    """
    The OIC's instruction or defect record, wherever it sits in the report.

    Position is no guide: it is usually the page after the photos, but in
    some reports it is a scan that is itself the last page carrying images.
    What marks it out is that it is not a photo page - those always label
    their pictures - and that it reads like an OIC record.

    A page naming a different defect is passed over first time round:
    batch scans sometimes leave another job's record at the back of a
    report, and accepting it would pass this report on somebody else's
    paperwork. The second pass drops that condition, so a record whose own
    reference is merely misread still counts.
    """
    candidates = [pno for pno in range(1, len(doc)) if not photo_page(doc[pno])]
    for skip_other_defects in (True, False):
        for pno in candidates:
            text = page_text_with_ocr(doc[pno])
            named = norm_ref(text)
            if skip_other_defects and own_ref and named and named != own_ref:
                continue
            if looks_like_oic_instruction(text):
                return {"found": True, "detail": f"OIC instruction/supporting page detected on page {pno + 1}"}
    if not candidates:
        return {"found": False, "detail": "Every page after the sketch holds photos; no OIC instruction found"}
    pages = ", ".join(str(pno + 1) for pno in candidates)
    return {"found": False, "detail": f"No clear OIC instruction on page {pages}"}


def parse_report(pdf_bytes, filename, expected_refs=None):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        ms = measurements(doc[0])
        pcs = pq_candidates(doc[0])
        ms = relevant_measurements(ms, pcs)
        ref = report_ref(doc, filename, expected_refs)
        return {
            "key": ref,
            "source": filename,
            "claimed_jobs": item_box_jobs(doc[0]),
            "sketch_jobs": attach_pq(ms, pcs),
            "after_photos": after_photos(doc),
            "photo_dims": [d for pno in range(1, len(doc)) for d in photo_distances(doc[pno], pno)],
            "oic": oic_record(doc, ref),
        }
    finally:
        doc.close()
