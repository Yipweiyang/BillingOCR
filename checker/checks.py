"""
Checks 1-4 on the records produced by a reader (see readers.py), and the
workflow that runs them. Nothing here opens a PDF or knows a page layout.
"""
from collections import Counter
from itertools import permutations

from .common import area_total, close, fmt_date, ocr_image
from .photos import (board_area_rescan, board_confirms, board_confirms_area, dims_match,
                     is_app_screenshot, load_photo, parse_timestamp)
from .readers import read_batch

PASS, FLAG, NA = "PASS", "FLAG", "N/A"
# The evidence could not settle it either way - typically hand-writing OCR
# could not read or did not agree - so a person should look.
REVIEW = "REVIEW"


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


def compare_board(item, readings, texts=()):
    """
    Check 2 for photo evidence: the dimensions hand-written on the site
    board against the mastersheet's areas. Hand-writing OCR is unreliable,
    so a board that cannot be read or does not agree asks for a person to
    look (REVIEW) instead of failing the item.
    """
    areas = item.get("areas") or []
    qty = item["jobs"][0]["qty"] if item["jobs"] else None
    if not areas:
        return NA, "No dimensions in the mastersheet for this item (e.g. billed per hour)"

    # QTY is printed to 2 dp, so 0.25 x 0.9 = 0.225 is billed as 0.23.
    total = area_total(areas)
    if not close(total, qty, tol=0.006):
        return FLAG, f"Mastersheet dimensions give {round(total, 3)} m2 but QTY is {qty}"

    def size(d):
        count = d.get("count", 1)
        return f"{d['length']:g} x {d['width']:g}" + (f" ({count} nos)" if count > 1 else "")

    listed = " + ".join(size(a) for a in areas)
    exact = [(r, a) for r in readings for a in areas if dims_match(a, r)]
    found = exact or [(r, a) for r in readings for a in areas if dims_match(a, r, loose=True)]

    if not found:
        # Nothing parsed cleanly. A board too garbled to read can still be
        # legible enough to confirm the dimension the mastersheet states.
        for t in texts:
            for a in areas:
                line = board_confirms_area(t["text"], a)
                if line:
                    return PASS, (f"Board on {t['label']} reads {line!r}, which confirms mastersheet "
                                  f"{listed} [board only partly legible]")
        # Last resort: read the board again, larger. Only the handful of
        # items nothing else settled reach this, so the cost stays small.
        for t in texts:
            if not t.get("path"):
                continue
            for a in areas:
                line = board_area_rescan(t, a)
                if line:
                    return PASS, (f"Board on {t['label']} reads {line!r} re-read at higher resolution, "
                                  f"which confirms mastersheet {listed} [board only partly legible]")
        if not readings:
            return REVIEW, f"No dimensions readable on the board photos (mastersheet {listed})"
        read = ", ".join(f"{size(r)} on {r['label']}" for r in readings)
        return REVIEW, f"Board reads {read}; mastersheet has {listed}"

    # Prefer a board whose tile count also agrees. Counts are only a note:
    # a long run of tiles is often split over several boards.
    found.sort(key=lambda ra: ra[0].get("count", 1) != ra[1].get("count", 1))
    r, a = found[0]
    notes = []
    if not exact:
        notes.append("decimal point unclear on board")
    if r.get("count", 1) > 1 and r["count"] != a.get("count", 1):
        notes.append(f"board shows {r['count']} nos, mastersheet {a.get('count', 1)}")
    seen = len({id(a) for _, a in found})
    if seen < len(areas):
        notes.append(f"{seen} of {len(areas)} areas seen on boards")
    detail = f"Board reads {size(r)} on {r['label']}, matching mastersheet {listed}"
    return PASS, detail + (" [" + "; ".join(notes) + "]" if notes else "")


def compare_jobs(item, evidence):
    """
    Check 2 compares the mastersheet against the quantities the contractor
    claims (the ITEM/QTY box on an RM report), and falls back to the sketch
    measurements only when those cannot be read.
    """
    if "board_dims" in evidence:
        return compare_board(item, evidence["board_dims"], evidence.get("board_texts", ()))

    master_jobs = item["jobs"]
    item_jobs, sketch = evidence["claimed_jobs"], evidence["sketch_jobs"]
    if item_jobs is None and sketch is None:
        return NA, "This format carries no quantities to compare"

    if not item_jobs:
        sketch = sketch or []
        errors = compare_measurements(master_jobs, sketch)
        if errors is None:
            return FLAG, (f"Quantity box unreadable and sketch does not align: "
                          f"master={len(master_jobs)} job(s), sketch={len(sketch)}")
        return (FLAG, "Quantity box unreadable; sketch differs: " + " | ".join(errors)) if errors else (
            PASS, f"Quantity box unreadable; {len(sketch)} sketch measurement(s) match the mastersheet")

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
        return FLAG, "Quantity box vs mastersheet: " + " | ".join(errors)

    notes = []
    if len(item_jobs) != len(master_jobs):
        notes.append(f"billed as {len(item_jobs)} line(s) where the mastersheet lists "
                     f"{len(master_jobs)} - totals agree")

    # TR388 sketches give no dimensions per mastersheet job, only area
    # arithmetic, so sketch_jobs is None there and the sums are checked instead.
    if sketch is not None:
        sketch_errors = compare_measurements(master_jobs, sketch)
        if sketch_errors is None:
            notes.append("sketch measurements could not be aligned, dimensions unverified")
        elif sketch_errors:
            return FLAG, "Sketch measurements differ: " + " | ".join(sketch_errors)

    total = sum(master_totals.values())
    detail = f"Quantity box matches mastersheet ({len(master_totals)} PQ, total {round(total, 2)})"
    suffix = " [" + "; ".join(notes) + "]" if notes else ""
    if evidence.get("sketch_errors"):
        return REVIEW, f"{detail}, but the sketch arithmetic is off: " + " | ".join(evidence["sketch_errors"]) + suffix
    return PASS, detail + suffix


# ---------- Check 3: AFTER photos ----------
# A crew that finishes late in the day may photograph the finished work
# the next morning, so an AFTER photo dated shortly after the completion
# date is normal. A photo dated *before* completion never is.
AFTER_PHOTO_GRACE_DAYS = 1


def lost_tens_digit(read, master_date):
    """
    True when OCR dropped the first digit of the day - "17 Apr" read as
    "7 Apr" - which happens when that digit sits over something white.
    """
    return (read.year, read.month) == (master_date.year, master_date.month) and \
        master_date.day >= 10 and read.day == master_date.day % 10


def photo_dates_match(photos, master_date):
    confirmed, wrong, unreadable, skipped, by_board, late, doubtful = [], [], [], [], [], [], []

    for photo in photos:
        label = photo["label"]
        text = photo.get("text")
        if text is None:
            text = ocr_image(load_photo(photo))

        if is_app_screenshot(text):
            skipped.append(label)
            continue

        cands = parse_timestamp(text)
        if any(d == master_date for d, _, _ in cands):
            confirmed.append(label)
        elif any(source == "watermark" for _, source, _ in cands):
            seen = sorted({d for d, source, _ in cands if source == "watermark"})
            gap = min((d - master_date).days for d in seen)
            if 0 <= gap <= AFTER_PHOTO_GRACE_DAYS:
                confirmed.append(label)
                late.append((label, min(seen), gap))
            elif board_confirms(load_photo(photo), master_date):
                confirmed.append(label)
                by_board.append(label)
            elif any(lost_tens_digit(d, master_date) for d in seen):
                doubtful.append((label, seen))
            else:
                wrong.append((label, seen))
        else:
            # No camera watermark: a cropped or unreadable timestamp.
            # Reported, but never enough on its own to fail the report.
            unreadable.append(label)

    notes = []
    if late:
        days = max(g for _, _, g in late)
        notes.append(f"photo(s) on {', '.join(label for label, _, _ in late)} taken "
                     f"{fmt_date(min(d for _, d, _ in late))}, {days} day after completion")
    if by_board:
        notes.append(f"completion board on {', '.join(by_board)} matches although the "
                     f"photo was taken on another day")
    if skipped:
        notes.append(f"{len(skipped)} EFMS screenshot(s) ignored ({', '.join(skipped)})")
    if unreadable:
        notes.append(f"no readable timestamp on {', '.join(unreadable)}")
    suffix = " [" + "; ".join(notes) + "]" if notes else ""

    if wrong:
        details = "; ".join(f"{label}=" + "/".join(fmt_date(d) for d in ds) for label, ds in wrong)
        return FLAG, f"Master completion date {fmt_date(master_date)} != {details}{suffix}"

    if doubtful and not confirmed:
        details = "; ".join(f"{label}=" + "/".join(fmt_date(d) for d in ds) for label, ds in doubtful)
        return REVIEW, (f"Watermark read as {details}, probably {fmt_date(master_date)} with its "
                        f"first digit lost - check the photo{suffix}")

    if not confirmed:
        return FLAG, f"No AFTER photo timestamp could be read{suffix}"

    return PASS, f"{len(confirmed)} of {len(photos)} AFTER photo(s) match {fmt_date(master_date)}{suffix}"


def check_after_dates(photos, master_date):
    if photos is None:
        return NA, "This format has no labelled AFTER photos"
    if master_date is None:
        return FLAG, "Mastersheet completion date is unreadable"
    if not photos:
        return FLAG, "No AFTER photos found"

    groups = {}
    for photo in photos:
        groups.setdefault(photo.get("group"), []).append(photo)
    if len(groups) == 1:
        return photo_dates_match(photos, master_date)

    # A folder shared by several S/Ns and split into dated subfolders: the
    # item passes when one subfolder's final photos match its completion date.
    results = {group or "folder root": photo_dates_match(ps, master_date) for group, ps in groups.items()}
    for group, (status, detail) in results.items():
        if status == PASS:
            return PASS, f"{detail} (in {group})"
    return FLAG, " | ".join(f"{group}: {detail}" for group, (_, detail) in results.items())


# ---------- Check 4: OIC instruction ----------
def check_oic(oic):
    if oic is None:
        return NA, "This format has no OIC instruction record"
    return (PASS if oic["found"] else FLAG), oic["detail"]


# ---------- Full workflow ----------
def run_checks(items, evidence, evidence_name="incident report", progress=None):
    counts = Counter(e["key"] for e in evidence if e["key"])
    by_key = {}
    for e in evidence:
        if e["key"]:
            by_key.setdefault(e["key"], []).append(e)

    # A TR388 mastersheet lists every sector while a bundle covers one, so a
    # sector with no evidence at all was not submitted rather than missing.
    covered = {items[k].get("sector") for k in by_key if k in items}
    not_submitted = {k for k, m in items.items()
                     if m.get("sector") and m["sector"] not in covered and k not in by_key}

    rows = []
    for done, (key, m) in enumerate(sorted(items.items(), key=lambda x: x[1]["sn"]), 1):
        if progress:
            progress(done, len(items))
        count = counts.get(key, 0)
        if key in not_submitted:
            c1, d1 = NA, f"No {evidence_name}s for sector {m['sector']} in this batch"
        elif count == 0:
            c1, d1 = FLAG, f"Missing {evidence_name}"
        elif count > 1:
            c1, d1 = FLAG, f"Duplicate {evidence_name}s: {count}"
        else:
            c1, d1 = PASS, f"Exactly one {evidence_name} found"

        c2 = c3 = c4 = NA if key in not_submitted else "NOT RUN"
        d2 = d3 = d4 = "Requires exactly one matched report"

        if count == 1:
            e = by_key[key][0]
            # Matched by key, but the evidence may describe another site.
            if e.get("location_note"):
                c1, d1 = REVIEW, f"Evidence found, but the site may not match: {e['location_note']}"
            c2, d2 = compare_jobs(m, e)
            c3, d3 = check_after_dates(e["after_photos"], m["date"])
            c4, d4 = check_oic(e["oic"])

        row = {"S/N": m["sn"], "Defect Ref": m.get("ref", key)}
        if "sector" in m:
            row["Sector"] = m["sector"]
        if "location" in m:
            row["Location"] = " ".join(x for x in (m["location"], m.get("landmark")) if x)
        row.update({
            "Check 1": c1, "Check 1 Detail": d1,
            "Check 2": c2, "Check 2 Detail": d2,
            "Check 3": c3, "Check 3 Detail": d3,
            "Check 4": c4, "Check 4 Detail": d4,
        })
        rows.append(row)

    item_keys = set(items)
    evidence_keys = {e["key"] for e in evidence if e["key"]}
    return {
        "rows": rows,
        "master_count": len(items),
        "uploaded_count": len(evidence),
        "missing": sorted(item_keys - evidence_keys - not_submitted),
        "duplicates": sorted(key for key, n in counts.items() if n > 1),
        "extra": sorted(evidence_keys - item_keys),
        "unreadable": [e["source"] for e in evidence if not e["key"]],
        "mapping": [
            {
                "Uploaded File": e["source"],
                "Extracted Defect Ref": e["key"] or "UNREADABLE",
            }
            for e in evidence
        ],
    }


def check_batch(batch_dir, progress=None):
    """
    Read a batch folder in whatever format it is, then run every check.

    progress(phase, done, total) is called throughout, so the caller can
    show how far along the two phases are.
    """
    def phase(name):
        return (lambda done, total: progress(name, done, total)) if progress else None

    items, evidence, evidence_name = read_batch(batch_dir, phase("Reading evidence"))
    return run_checks(items, evidence, evidence_name, phase("Running checks"))
