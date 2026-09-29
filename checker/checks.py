"""
Checks 1-5 on the records produced by a reader (see readers.py), and the
workflow that runs them. Nothing here opens a PDF or knows a page layout.
"""
import os
from collections import Counter
from itertools import permutations

from .common import area_total, close, fmt_date, ocr_image
from .photos import (board_area_rescan, board_confirms, board_confirms_area, dims_match,
                     is_app_screenshot, load_photo, parse_timestamp)
from .prices import area_band, price_list_for, same_unit, schedule_for
from .readers import read_batch

# Anything a person must look at is a FLAG. Its detail says whether the
# document is wrong or the checker could not read it well enough to tell.
PASS, FLAG, NA = "PASS", "FLAG", "N/A"


def sentence(text):
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def as_lines(heading, problems):
    """A heading, then each problem on a line of its own - the app shows them one per line."""
    return heading + "".join("\n• " + sentence(p) for p in problems)


def with_notes(detail, notes):
    """detail, followed by each side note on a line of its own."""
    return sentence(detail) + "".join("\nNote: " + sentence(n) for n in notes)


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


# Trying every permutation is exact but factorial: 8 jobs is 40 thousand
# orderings, 12 is 479 million. Items that large do not occur - the most
# seen in a batch so far is 2 - so the exact search is kept for the sizes
# that happen and a greedy pairing takes over beyond it, rather than the
# run stopping dead on one unusual item.
MAX_EXACT_ALIGN = 8


def best_alignment(master_jobs, sketch_jobs):
    """The order of sketch_jobs that best matches master_jobs, as indices."""
    n = len(master_jobs)
    if n <= MAX_EXACT_ALIGN:
        return min(permutations(range(n)),
                   key=lambda p: sum(measurement_distance(master_jobs[i], sketch_jobs[p[i]])
                                     for i in range(n)))

    free, perm = set(range(n)), []
    for i in range(n):
        j = min(free, key=lambda j: measurement_distance(master_jobs[i], sketch_jobs[j]))
        free.discard(j)
        perm.append(j)
    return perm


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

    perm = best_alignment(master_jobs, sketch_jobs)

    errors = []
    for i in range(n):
        a, b = master_jobs[i], sketch_jobs[perm[i]]
        e = [f"{f} {a[f]} on the mastersheet but {b[f]} on the sketch"
             for f in ("length", "width", "qty") if not close(a[f], b[f])]
        if e:
            errors.append(f"job {i+1} has " + ", ".join(e))
    return errors


def compare_board(item, readings, texts=()):
    """
    Check 2 for photo evidence: the dimensions hand-written on the site
    board against the mastersheet's areas. Hand-writing OCR is unreliable,
    so a board that cannot be read or does not agree is flagged as
    unreadable, not as wrong.
    """
    areas = item.get("areas") or []
    qty = item["jobs"][0]["qty"] if item["jobs"] else None
    if not areas:
        return NA, "The mastersheet gives no dimensions for this item (e.g. it is billed per hour)."

    # QTY is printed to 2 dp, so 0.25 x 0.9 = 0.225 is billed as 0.23.
    total = area_total(areas)
    if not close(total, qty, tol=0.006):
        return FLAG, (f"Mastersheet error: its dimensions add up to {round(total, 3)} m2, "
                      f"but it bills a quantity of {qty}.")

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
                    return PASS, (f"The site board on {t['label']} shows {line!r}, matching the "
                                  f"mastersheet's {listed}. Note: the board is only partly legible.")
        # Last resort: read the board again, larger. Only the handful of
        # items nothing else settled reach this, so the cost stays small.
        for t in texts:
            if not t.get("path"):
                continue
            for a in areas:
                line = board_area_rescan(t, a)
                if line:
                    return PASS, (f"The site board on {t['label']} shows {line!r} (read on a zoomed-in "
                                  f"second pass), matching the mastersheet's {listed}. "
                                  f"Note: the board is only partly legible.")
        if not readings:
            return FLAG, (f"Could not read the handwritten dimensions on the site board photos. "
                          f"Please check the photos show {listed}.")
        read = ", ".join(f"{size(r)} on {r['label']}" for r in readings)
        return FLAG, (f"The site board seems to show {read}, but the mastersheet has {listed}. "
                      f"The handwriting may have been misread - please check the photos.")

    # Prefer a board whose tile count also agrees. Counts are only a note:
    # a long run of tiles is often split over several boards.
    found.sort(key=lambda ra: ra[0].get("count", 1) != ra[1].get("count", 1))
    r, a = found[0]
    notes = []
    if not exact:
        notes.append("the decimal point on the board is unclear")
    if r.get("count", 1) > 1 and r["count"] != a.get("count", 1):
        notes.append(f"the board shows {r['count']} nos where the mastersheet has {a.get('count', 1)}")
    seen = len({id(a) for _, a in found})
    if seen < len(areas):
        notes.append(f"only {seen} of the {len(areas)} areas appear on a board")
    return PASS, with_notes(f"The site board on {r['label']} shows {size(r)}, "
                            f"matching the mastersheet's {listed}", notes)


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
        return NA, "This report format has no quantities to compare."

    if not item_jobs:
        sketch = sketch or []
        errors = compare_measurements(master_jobs, sketch)
        if errors is None:
            return FLAG, (f"Could not read the report's quantity box, and the sketch cannot stand in for it: "
                          f"the mastersheet has {len(master_jobs)} job(s) but the sketch has {len(sketch)}. "
                          f"Please check the report.")
        if errors:
            return FLAG, as_lines("Could not read the report's quantity box, and the sketch disagrees with the "
                                  "mastersheet:", errors)
        return PASS, (f"Could not read the report's quantity box, but all {len(sketch)} sketch "
                      f"measurement(s) match the mastersheet.")

    master_totals, report_totals = pq_totals(master_jobs), pq_totals(item_jobs)

    errors = []
    for pq in sorted(set(master_totals) | set(report_totals)):
        a, b = master_totals.get(pq), report_totals.get(pq)
        if a is None:
            errors.append(f"{pq} is claimed in the report ({b}) but not billed on the mastersheet")
        elif b is None:
            errors.append(f"{pq} is billed on the mastersheet ({a}) but not claimed in the report")
        elif not close(a, b):
            errors.append(f"{pq} is {a} on the mastersheet but {b} in the report")
    if errors:
        return FLAG, as_lines("Quantities do not match:", errors)

    notes = []
    if len(item_jobs) != len(master_jobs):
        notes.append(f"the report splits this into {len(item_jobs)} line(s) where the mastersheet has "
                     f"{len(master_jobs)}, but the totals agree")

    # TR388 sketches give no dimensions per mastersheet job, only area
    # arithmetic, so sketch_jobs is None there and the sums are checked instead.
    if sketch is not None:
        sketch_errors = compare_measurements(master_jobs, sketch)
        if sketch_errors is None:
            notes.append("the sketch measurements could not be paired with the mastersheet jobs, "
                         "so lengths and widths were not checked")
        elif sketch_errors:
            return FLAG, as_lines("Sketch measurements do not match the mastersheet:", sketch_errors)

    if evidence.get("sketch_errors"):
        return FLAG, with_notes(as_lines("Sketch calculation error (the quantities themselves match the mastersheet):",
                                         evidence["sketch_errors"]), notes)
    total = sum(master_totals.values())
    return PASS, with_notes(f"Quantities match the mastersheet ({len(master_totals)} PQ item(s), "
                            f"total {round(total, 2)})", notes)


def compare_after_dims(evidence):
    """
    The distances typed over the AFTER photos - beside a tape, or along the
    box drawn round the finished repair - against the QUANTITY the report's
    first page gives: the 'L x W = Q' sizes on the sketch, and the QTY box.
    Returns None when the format has no such photos, else (agrees, note),
    agrees being None when no AFTER photo is marked.

    Two distances agree when they are a sketch size's length and width, or
    multiply to one of its quantities. A lone distance agrees when it is
    one side of a sketch size.
    """
    if "photo_dims" not in evidence:
        return None
    after = [p for p in evidence["photo_dims"] if p["label"].endswith("AFTER")]
    if not after:
        return None, "AFTER photos: no measurements are marked on them, so they were not compared."

    sizes = evidence.get("sketch_sizes") or []
    claimed = [j["qty"] for j in evidence.get("claimed_jobs") or [] if j.get("qty") is not None]
    if not sizes and not claimed:
        return False, ("AFTER photos: they are marked with measurements, but the quantity on the report's "
                       "first page could not be read. Please compare them by hand.")
    # What an area can match, and how to name it.
    areas = [(s["qty"], f"the sketch's {s['length']:g} x {s['width']:g}") for s in sizes] + \
        [(q, f"QTY {q:g}") for q in claimed] + \
        ([(sum(claimed), f"the total QTY {sum(claimed):g}")] if len(claimed) > 1 else [])

    def size_of(a, b):
        return next((s for s in sizes if close(a, s["length"]) and close(b, s["width"])), None)

    agree, disagree = [], []
    for p in after:
        vs, where = p["values"], f"the AFTER photo on {p['label'].split()[0]}"
        shown = f"{' x '.join(f'{v:g}' for v in vs)}m on {where}"
        pairs = list(permutations(vs, 2))
        pair = next(((a, b) for a, b in pairs if size_of(a, b)), None)
        area = None if pair else next(((a, b, name) for a, b in pairs for q, name in areas
                                       if close(a * b, q, tol=0.006)), None)
        side = None if pair or area or len(vs) != 1 else \
            next((s for s in sizes if close(vs[0], s["length"]) or close(vs[0], s["width"])), None)
        if pair:
            agree.append(f"{pair[0]:g} x {pair[1]:g}m on {where} is the sketch's {pair[0]:g} x {pair[1]:g}")
        elif area:
            agree.append(f"{area[0]:g} x {area[1]:g}m on {where} gives {area[0] * area[1]:g} m2, the area of {area[2]}")
        elif side:
            agree.append(f"{shown} is one side of the sketch's {side['length']:g} x {side['width']:g}")
        else:
            disagree.append(shown)

    if disagree:
        listed = " + ".join(f"{s['length']:g} x {s['width']:g}" for s in sizes)
        quantity = " and ".join(x for x in (listed and f"the sketch's {listed}",
                                            claimed and f"QTY {' + '.join(f'{q:g}' for q in claimed)}") if x)
        return False, with_notes(as_lines(f"AFTER photos: their measurements do not match {quantity}:",
                                          disagree),
                                 [f"these do match: {'; '.join(agree)}"] if agree else [])
    return True, f"AFTER photos agree with the report: {'; '.join(agree)}."


def triple_check(status, detail, evidence):
    """
    Check 2 as a chain of three: the mastersheet against the report's
    quantities (compare_jobs), then those quantities against what the
    AFTER photos measure. A disagreement anywhere is a FLAG; photos with
    nothing marked leave the first two to stand on their own.
    """
    result = compare_after_dims(evidence)
    if result is None or status == NA:
        return status, detail
    agrees, note = result
    return (FLAG if agrees is False else status), f"{detail}\n{note}"


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
        notes.append(f"the photo(s) on {', '.join(label for label, _, _ in late)} were taken "
                     f"{fmt_date(min(d for _, d, _ in late))}, {days} day after completion, which is allowed")
    if by_board:
        notes.append(f"the photo on {', '.join(by_board)} was taken on another day, but the "
                     f"completion board in it shows the right date")
    if skipped:
        notes.append(f"{len(skipped)} EFMS screenshot(s) were skipped ({', '.join(skipped)})")
    if unreadable:
        notes.append(f"the date stamp could not be read on {', '.join(unreadable)}")

    if wrong:
        details = [f"{label} is dated " + " / ".join(fmt_date(d) for d in ds) for label, ds in wrong]
        return FLAG, with_notes(as_lines(f"Wrong date: the mastersheet says the work was completed on "
                                         f"{fmt_date(master_date)}, but:", details), notes)

    if doubtful and not confirmed:
        details = "; ".join(f"{label} as " + " / ".join(fmt_date(d) for d in ds) for label, ds in doubtful)
        return FLAG, with_notes(f"Date stamp unclear: read {details}. This is probably "
                                f"{fmt_date(master_date)} with the first digit misread - please check the photo",
                                notes)

    if not confirmed:
        return FLAG, with_notes("Could not read the date stamp on any AFTER photo. "
                                "Please check the photo dates by hand", notes)

    return PASS, with_notes(f"{len(confirmed)} of {len(photos)} AFTER photo(s) are dated "
                            f"{fmt_date(master_date)}, as on the mastersheet", notes)


def check_after_dates(photos, master_date):
    if photos is None:
        return NA, "This report format has no labelled AFTER photos."
    if master_date is None:
        return FLAG, "Could not read the completion date on the mastersheet. Please check it by hand."
    if not photos:
        return FLAG, "No AFTER photos were found in the report."

    # No photo was labelled After, so the reader handed over the last photo
    # instead. A wrong label is still a mistake, so this never passes.
    stand_in = next((p for p in photos if p.get("stand_in")), None)
    if stand_in:
        status, detail = photo_dates_match(photos, master_date)
        if status == PASS:
            return FLAG, (f"No photo is labelled After. The last photo (labelled {stand_in['stand_in']}) "
                          f"shows a date of {fmt_date(master_date)}, matching the completion date, "
                          f"so it is probably the After photo with the wrong label. Please confirm.")
        return FLAG, f"No photo is labelled After, so {stand_in['label']} was checked instead. {detail}"

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
            return PASS, f"In {group}: {detail}"
    return FLAG, "\n".join(f"In {group}: {detail}" for group, (_, detail) in results.items())


# ---------- Check 4: OIC instruction ----------
def check_oic(oic):
    if oic is None:
        return NA, "This report format has no OIC instruction page."
    return (PASS if oic["found"] else FLAG), oic["detail"]


# ---------- Check 5: PQ items and prices ----------
def money(x):
    return f"${x:,.2f}"


def fmt_band(band):
    above, up_to = band
    if above is None:
        return f"areas up to {up_to:g} m2"
    if up_to is None:
        return f"areas over {above:g} m2"
    return f"areas over {above:g} m2 up to {up_to:g} m2"


def check_prices(jobs, completed, price_list):
    """
    Each mastersheet line against the contract's rate schedule: it is billed
    under the right PQ item, at the scheduled unit rate, and QTY x rate is
    the total billed.

    Items like PQ30.1.1/.2/.3 are one repair priced by the area of the
    location, so a line's area must fall in its item's range. Each line is
    one location: a location split across ranges is billed as one line per
    range, as RM206 NE4-E-43055 does.

    A schedule has a period - TR388's is a contract extension - and work
    done outside it was priced under another schedule, so its rates are
    shown but not judged. The wrong item for the area, arithmetic that does
    not add up, or a unit that is not the item's, is wrong whichever
    schedule applies - an item's area range does not change between them.
    """
    contract = (price_list or {}).get("contract")
    schedules = (price_list or {}).get("schedules") or []
    rejected = (price_list or {}).get("rejected") or []
    where = (price_list or {}).get("where") or "in the price folder"
    # A contract accumulates schedules, so the one covering this line's
    # completion date is the one it should have been billed against.
    schedule = schedule_for(schedules, completed)
    if schedule is None:
        detail = f"No rate schedule for {contract or 'this contract'} {where}, so PQ items and prices were not checked"
        if rejected:
            detail += ". Files that could not be used: " + "; ".join(f"{name} ({why})" for name, why in rejected)
        return NA, detail + "."
    if not jobs:
        return NA, "No billed lines to price."

    start, end = schedule["valid_from"], schedule["valid_to"]
    outside = completed is not None and ((start and completed < start) or (end and completed > end))
    wrong, unjudged, total = [], [], 0.0

    for j in jobs:
        pq, qty, billed, unit = j["pq"], j["qty"], j.get("rate"), j.get("unit")
        entry = schedule["items"].get(pq.upper())
        if entry is None:
            wrong.append(f"{pq} is not an item in the {contract} rate schedule")
            continue
        band = area_band(entry["description"])
        if band and qty is not None:
            above, up_to = band
            if (above is not None and qty <= above) or (up_to is not None and qty > up_to):
                wrong.append(f"{pq} is for {fmt_band(band)}, but this line bills {qty:g} m2")
        rate = entry["rate"]
        if billed is None:
            wrong.append(f"could not read the unit rate for {pq} (it should be {money(rate)}) - please check it by hand")
        elif not close(billed, rate, tol=0.005):
            (unjudged if outside else wrong).append(
                f"{pq} is billed at {money(billed)}, but the schedule rate is {money(rate)}")
        if unit and entry["unit"] and not same_unit(unit, entry["unit"]):
            wrong.append(f"{pq} is billed per {unit}, but the schedule prices it per {entry['unit']}")

        amount = j.get("amount")
        if amount is not None and qty is not None and billed is not None:
            # Totals are printed to the cent, so 1.68 x $36.10 = $60.648 is billed as $60.65.
            if not close(amount, qty * billed, tol=0.011):
                wrong.append(f"{pq} total is {money(amount)}, but {qty:g} x {money(billed)} "
                             f"= {money(qty * billed)}")
            total += amount

    notes = []
    if outside:
        held = (f", and none of the {len(schedules)} {contract} schedules {where} covers that date"
                if len(schedules) > 1 else "")
        notes.append(f"the work was completed {fmt_date(completed)}, outside the {contract} schedule's period "
                     f"({fmt_date(start)} to {fmt_date(end)}){held}, so these rates were not checked")
        notes.extend(unjudged)

    if wrong:
        return FLAG, with_notes(as_lines("Price problem:", wrong), notes)
    if unjudged:
        return NA, as_lines(sentence(notes[0]), unjudged)
    if len(schedules) > 1:
        notes.append(f"priced against {schedule['source']}")
    return PASS, with_notes(f"All {len(jobs)} line(s) are billed under the right PQ item at the {contract} schedule rates, "
                            f"total {money(total)}", notes)


# ---------- Full workflow ----------
def run_checks(items, evidence, evidence_name="incident report", progress=None, price_list=None):
    """price_list is prices.price_list_for(mastersheet); without one, check 5 is N/A."""
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
            c1, d1 = NA, f"Sector {m['sector']} was not part of this batch."
        elif count == 0:
            c1, d1 = FLAG, f"Missing: no {evidence_name} was uploaded for this item."
        elif count > 1:
            c1, d1 = FLAG, f"Duplicate: {count} {evidence_name}s were uploaded for this item; there should be one."
        else:
            c1, d1 = PASS, f"One {evidence_name} found for this item."

        c2 = c3 = c4 = NA if key in not_submitted else "NOT RUN"
        d2 = d3 = d4 = ("Not checked, because this item needs exactly one "
                        f"{evidence_name} (see Check 1).")

        if count == 1:
            e = by_key[key][0]
            # Matched by key, but the evidence may describe another site.
            if e.get("location_note"):
                c1, d1 = FLAG, (f"Location may not match: {e['location_note']}. "
                                f"Please confirm this {evidence_name} is for the right site.")
            c2, d2 = triple_check(*compare_jobs(m, e), e)
            c3, d3 = check_after_dates(e["after_photos"], m["date"])
            c4, d4 = check_oic(e["oic"])

        # The mastersheet alone is priced, so this runs whatever the evidence.
        c5, d5 = check_prices(m["jobs"], m["date"], price_list)

        row ={"S/N": m["sn"], "Defect Ref": m.get("ref", key)}
        if "sector" in m:
            row["Sector"] = m["sector"]
        if "location" in m:
            row["Location"] = " ".join(x for x in (m["location"], m.get("landmark")) if x)
        row.update({
            "Check 1": c1, "Check 1 Detail": d1,
            "Check 2": c2, "Check 2 Detail": d2,
            "Check 3": c3, "Check 3 Detail": d3,
            "Check 4": c4, "Check 4 Detail": d4,
            "Check 5": c5, "Check 5 Detail": d5,
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

    items, evidence, evidence_name, master = read_batch(batch_dir, phase("Reading evidence"))
    price_list = price_list_for(master)
    return run_checks(items, evidence, evidence_name, phase("Running checks"), price_list)
