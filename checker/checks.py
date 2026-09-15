"""Checks 1-4 and the full workflow that runs them for one mastersheet."""
import re
from collections import Counter
from itertools import permutations

from .common import close, fmt_date, norm_ref, ocr_image, page_text_with_ocr
from .mastersheet import parse_master
from .photos import after_images, board_confirms, is_app_screenshot, native_image, parse_timestamp
from .report import parse_report


# ---------- Check 2: quantities ----------
def pq_totals(jobs):
    totals = {}
    for j in jobs:
        totals[j["pq"]] = totals.get(j["pq"], 0.0) + (j["qty"] or 0.0)
    return totals


def measurement_distance(a, b):
    score = 0.0
    for f in ("length", "width", "qty"):
        score += 100 if a.get(f) is None or b.get(f) is None else abs(a[f] - b[f])
    return score


def compare_measurements(master_jobs, sketch_jobs):
    """
    Cross-check the individual 'L x W = Q' lines in the sketch against the
    mastersheet. The ITEM box carries no dimensions, so this is the only
    place length and width can be verified. Returns None when the two
    sides cannot be aligned one-to-one.
    """
    n = len(master_jobs)
    if n == 0 or len(sketch_jobs) != n:
        return None

    perm = min(permutations(range(n)),
               key=lambda p: sum(measurement_distance(master_jobs[i], sketch_jobs[p[i]]) for i in range(n)))

    errors = []
    for i in range(n):
        a, b = master_jobs[i], sketch_jobs[perm[i]]
        e = [f"{f} {a[f]} vs {b[f]}" for f in ("length", "width", "qty") if not close(a[f], b[f])]
        if e:
            errors.append(f"Job {i+1}: " + ", ".join(e))
    return errors


def compare_jobs(master_jobs, report):
    """
    Check 2 compares the mastersheet against the ITEM/QTY box on page 1,
    which is the contractor's actual claim, and falls back to the sketch
    measurements only when that box cannot be read.
    """
    item_jobs = report.get("item_jobs")
    if not item_jobs:
        sketch = report["jobs"]
        errors = compare_measurements(master_jobs, sketch)
        if errors is None:
            return False, (f"Quantity box unreadable and sketch does not align: "
                           f"master={len(master_jobs)} job(s), sketch={len(sketch)}")
        return (False, "Quantity box unreadable; sketch differs: " + " | ".join(errors)) if errors else (
            True, f"Quantity box unreadable; {len(sketch)} sketch measurement(s) match the mastersheet")

    master_totals, report_totals = pq_totals(master_jobs), pq_totals(item_jobs)

    errors = []
    for pq in sorted(set(master_totals) | set(report_totals)):
        a, b = master_totals.get(pq), report_totals.get(pq)
        if a is None:
            errors.append(f"{pq} claimed as {b} but not in mastersheet")
        elif b is None:
            errors.append(f"{pq} in mastersheet ({a}) but not claimed")
        elif not close(a, b):
            errors.append(f"{pq} quantity {a} vs {b}")
    if errors:
        return False, "Quantity box vs mastersheet: " + " | ".join(errors)

    notes = []
    if len(item_jobs) != len(master_jobs):
        notes.append(f"billed as {len(item_jobs)} line(s) where the mastersheet lists "
                     f"{len(master_jobs)} - totals agree")

    sketch_errors = compare_measurements(master_jobs, report["jobs"])
    if sketch_errors is None:
        notes.append("sketch measurements could not be aligned, dimensions unverified")
    elif sketch_errors:
        return False, "Sketch measurements differ: " + " | ".join(sketch_errors)

    total = sum(master_totals.values())
    detail = f"Quantity box matches mastersheet ({len(master_totals)} PQ, total {round(total, 2)})"
    return True, detail + (" [" + "; ".join(notes) + "]" if notes else "")


# ---------- Check 3: AFTER photos ----------
# A crew that finishes late in the day may photograph the finished work
# the next morning, so an AFTER photo dated shortly after the completion
# date is normal. A photo dated *before* completion never is.
AFTER_PHOTO_GRACE_DAYS = 1


def check_after_dates(doc, master_date):
    if master_date is None:
        return False, "Mastersheet completion date is unreadable", None

    confirmed, wrong, unreadable, skipped, by_board, late = [], [], [], [], [], []
    total = 0
    last_after_page = None

    for pno, page in enumerate(doc):
        xrefs = after_images(page)
        if not xrefs:
            continue
        last_after_page = pno
        for xref in xrefs:
            total += 1
            image = native_image(doc, xref)
            text = ocr_image(image)

            if is_app_screenshot(text):
                skipped.append(pno + 1)
                continue

            cands = parse_timestamp(text)
            if any(d == master_date for d, _, _ in cands):
                confirmed.append(pno + 1)
            elif any(source == "watermark" for _, source, _ in cands):
                seen = sorted({d for d, source, _ in cands if source == "watermark"})
                gap = min((d - master_date).days for d in seen)
                if 0 <= gap <= AFTER_PHOTO_GRACE_DAYS:
                    confirmed.append(pno + 1)
                    late.append((pno + 1, min(seen), gap))
                elif board_confirms(image, master_date):
                    confirmed.append(pno + 1)
                    by_board.append(pno + 1)
                else:
                    wrong.append((pno + 1, seen))
            else:
                # No camera watermark: a cropped or unreadable timestamp.
                # Reported, but never enough on its own to fail the report.
                unreadable.append(pno + 1)

    if total == 0:
        return False, "No AFTER photos found", last_after_page

    notes = []
    if late:
        days = max(g for _, _, g in late)
        pages = ", p".join(str(p) for p, _, _ in late)
        notes.append(f"photo(s) on p{pages} taken {fmt_date(min(d for _, d, _ in late))}, "
                     f"{days} day after completion")
    if by_board:
        notes.append(f"completion board on p{', p'.join(map(str, by_board))} matches although the "
                     f"photo was taken on another day")
    if skipped:
        notes.append(f"{len(skipped)} EFMS screenshot(s) ignored (p{', p'.join(map(str, skipped))})")
    if unreadable:
        notes.append(f"no readable timestamp on p{', p'.join(map(str, unreadable))}")
    suffix = " [" + "; ".join(notes) + "]" if notes else ""

    if wrong:
        details = "; ".join(f"p{p}=" + "/".join(fmt_date(d) for d in ds) for p, ds in wrong)
        return False, f"Master completion date {fmt_date(master_date)} != {details}{suffix}", last_after_page

    if not confirmed:
        return False, f"No AFTER photo timestamp could be read{suffix}", last_after_page

    return True, f"{len(confirmed)} of {total} AFTER photo(s) match {fmt_date(master_date)}{suffix}", last_after_page


# ---------- Check 4: OIC instruction page ----------
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


def check_oic(doc, last_after_page):
    if last_after_page is None:
        return False, "Cannot locate OIC page because no AFTER page was found"
    candidate = last_after_page + 1
    if candidate >= len(doc):
        return False, "No page immediately after the final AFTER-photo page"
    text = page_text_with_ocr(doc[candidate])
    if looks_like_oic_instruction(text):
        return True, f"OIC instruction/supporting page detected on page {candidate + 1}"
    return False, f"No clear OIC instruction detected on page {candidate + 1}"


# ---------- Full workflow ----------
def run_checks(master_bytes, report_uploads):
    master = parse_master(master_bytes)
    reports = [
        parse_report(x.getvalue(), x.name, master.keys())
        for x in report_uploads
    ]
    counts = Counter(r["ref"] for r in reports if r["ref"])
    by_ref = {}
    for r in reports:
        if r["ref"]:
            by_ref.setdefault(r["ref"], []).append(r)

    rows = []
    for ref, m in sorted(master.items(), key=lambda x: x[1]["sn"]):
        count = counts.get(ref, 0)
        if count == 0:
            c1, d1 = "FLAG", "Missing incident report"
        elif count > 1:
            c1, d1 = "FLAG", f"Duplicate incident reports: {count}"
        else:
            c1, d1 = "PASS", "Exactly one incident report found"

        c2 = c3 = c4 = "NOT RUN"
        d2 = d3 = d4 = "Requires exactly one matched report"

        if count == 1:
            r = by_ref[ref][0]
            ok, d2 = compare_jobs(m["jobs"], r)
            c2 = "PASS" if ok else "FLAG"

            ok, d3, last_after = check_after_dates(r["doc"], m["date"])
            c3 = "PASS" if ok else "FLAG"

            ok, d4 = check_oic(r["doc"], last_after)
            c4 = "PASS" if ok else "FLAG"

        rows.append({
            "S/N": m["sn"], "Defect Ref": ref,
            "Check 1": c1, "Check 1 Detail": d1,
            "Check 2": c2, "Check 2 Detail": d2,
            "Check 3": c3, "Check 3 Detail": d3,
            "Check 4": c4, "Check 4 Detail": d4,
        })

    master_refs = set(master)
    report_refs = {r["ref"] for r in reports if r["ref"]}
    result = {
        "rows": rows,
        "master_count": len(master),
        "uploaded_count": len(report_uploads),
        "missing": sorted(master_refs - report_refs),
        "duplicates": sorted(ref for ref, n in counts.items() if n > 1),
        "extra": sorted(report_refs - master_refs),
        "unreadable": [r["filename"] for r in reports if not r["ref"]],
        "mapping": [
            {
                "Uploaded File": r["filename"],
                "Extracted Defect Ref": r["ref"] or "UNREADABLE",
            }
            for r in reports
        ],
    }
    for r in reports:
        r["doc"].close()
    return result
