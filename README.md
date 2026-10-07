# BillingOCR

Checks a contractor's billing claim against the evidence supplied with it.

The claim is a **mastersheet** PDF listing repairs. The evidence is either one **incident
report** PDF per defect, or a folder of **site photos** per item. This reads both and reports
where they disagree. It decides nothing — a person still judges what a disagreement means.

## Install and run

Needs Python 3.12.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
.venv/bin/python -m streamlit run app.py       # Windows: .venv\Scripts\python
```

Then upload a mastersheet and its evidence - one incident report PDF per defect, a bundle PDF
per sector, or a ZIP of the photo folders - plus the contract's price schedule for check 5. The format is recognised from the mastersheet,
and a batch whose files do not match it is refused with an explanation rather than checked as
the wrong format.

A batch folder under `data/` can also be checked in code, via `check_batch()`.

The first run over new photos takes a few minutes, because every image is read by OCR. The
results are remembered, so running the same batch again takes seconds.

## Data layout

Only used by `check_batch()`, not by the app. One folder per batch, holding the mastersheet
and its evidence. **The mastersheet PDF must be
named after its folder** — that is how it is found.

```
data/
  RM206- SP _CFM_NE4-Jan 26/
    RM206- SP _CFM_NE4-Jan 26.pdf     mastersheet
    1. NE4-W-42204.pdf                one incident report per defect
    2. NE4-W-42409.pdf
  SP-717 (SA). TR387 APR 2026/
    SP-717 (SA). TR387 APR 2026.pdf   mastersheet
    01. WHAMPOA ROAD LP 7/            one folder per item, named by location
    05-06. SERANGOON ROAD LP 71/      a folder may cover several items
```

`data/` is not committed.

## What it checks

| Check | Question |
|---|---|
| 1 | Is there evidence for this line, exactly once? |
| 2 | Do the quantities agree with what was claimed? |
| 3 | Were the AFTER photos taken when the mastersheet says the work finished? |
| 4 | Is the officer's instruction in the report? |
| 5 | Is each line billed under the right PQ item for its area (≤ 2 m², 2–5 m², > 5 m²), at the contract's scheduled unit rate, and does QTY × rate give its total? |

Check 2 is a chain of three: the mastersheet against the report's ITEM/QTY box (and its sketch),
then the report's QUANTITY against the distances typed over the AFTER photos (beside a measuring
tape, or along the box drawn around the repair). Two distances agree when they are a sketch line's
length and width, or when they multiply to one of its quantities; a single distance agrees when it
is one side of a sketch line. A photo of several areas need not show both sides of each, so every
distance only has to be a side of some sketch line, no lone side being used twice. An area the sketch
marks "Less" (a grating inside the repair, say) is a deduction: it is taken off when the sketch's
areas are added up against the billed quantity. A disagreement anywhere is a FLAG. AFTER photos with no measurements
marked leave the first comparison to stand on its own, and the detail says so. TR387 has no report
page, so its site board is compared with the mastersheet instead.
Only text shown on the page counts: a QUANTITY line hidden under the map (left over from a copied
report) is ignored, and a QUANTITY box whose text layer cannot be read is OCR'd.

Check 3 reads each AFTER photo's date wherever the report shows it: burned into the picture by the
camera, or typed over it as text. A burned-in stamp that reads as another date is read again
enlarged before it is called wrong, since one over trees or sky reads differently from one scale to
the next; a re-read giving the mastersheet date passes, with a note saying the stamp is hard to read.
An RM report's own "Date Completed" on its first page is compared
with the mastersheet too, and a disagreement is a FLAG; it never stands in for a photo date that
could not be read.

Check 5 needs only the mastersheet, so it runs even where the evidence is missing. The
rates and the area each PQ item is for come from the contract's rate schedule, which
the user uploads (see below). Each mastersheet line is taken as one location: a location split across
bands is billed as one line per band.

Results are **PASS**, **FLAG** (someone needs to look — the detail says whether the
evidence contradicts the mastersheet or could not be read well enough to decide, usually
hand-writing), **N/A** (this format has nothing to check here) or **NOT RUN** (check 1 found no single piece
of evidence, so the rest cannot run).

## Formats

| Contract | Evidence | Items identified by |
|---|---|---|
| RM205, RM206 | One report PDF per defect | Defect reference, e.g. `NE4-E-42446` |
| TR387 / SP-717 | A folder of site photos per item | S/N — these rows carry no defect reference |
| TR388 (CFM) | One bundle PDF per sector: OIC screenshot, sketch, photo sheet per incident | D.No, e.g. `15612W` — plus the landmark (`Lp 46`) where a D.No repeats |

A TR388 mastersheet lists every sector, so rows for a sector with no bundle in the folder report
N/A rather than missing.

Photo batches have no OIC page, so check 4 reports N/A, and quantities come from the
dimensions hand-written on the board in the photos. A batch matching neither format stops
with an error rather than guessing.

## Rate schedules

Check 5 reads the contract's Bill of Quantities, which the user uploads in the app's third box
alongside the mastersheet. Price files hold contract rates, so like the batches they are never
committed and the app saves none to disk: whoever runs a check supplies them. Several can be
uploaded at once - an original and its extensions, or schedules for other contracts, which are
simply not used. These shapes are read:

```
  RM205 Consol Doc (Vol. 1).pdf           the whole contract document, as a PDF
  RM206_Rate_Sec A & B 1.xls              .xls workbook
  TR387_ESTIMATION (EL) (r3) 2.xlsb       .xlsb price list
  TR387 - SA - Annex B.pdf                PDF with a text layer
  TR388_CHC_Price_SOT_Extension.pdf       PDF with a text layer
```

Only Section B — *Provisional quantities for ad hoc works* — is read, since that is what the
mastersheet's PQ items refer to. An *option bill* repeating Section B at other rates (RM205
has one) is skipped: mastersheets bill the main one. A schedule is matched to a batch by the contract code in the
mastersheet (`RM206`). The TR388 mastersheet names no contract, so it is matched by region
instead: its NW1–NW3 sectors are the schedule's "North West sector".

A batch with no schedule uploaded for its contract reports N/A. A schedule that states a period, like
the TR388 2026–2028 extension, only judges the rates of work completed inside it. For earlier
work the rates are shown but not judged, and check 5 reports N/A unless the arithmetic or a
unit is wrong. The PQ item's area range is judged either way, since it does not change
between schedules.

**A contract may have several schedules** — an original and its extensions — and all of them
can be uploaded together. Each line is priced against the schedule covering its completion
date, so one batch spanning a renewal is judged correctly throughout, and the result names the
schedule it used.

A price file that cannot be read, names no contract, or yields no Section B items is **not**
silently skipped: the app names it and says why, so a sheet in an unexpected shape shows up as
a message rather than as every row quietly reporting N/A.

A PDF schedule is read by column: each page's ruled ITEM / DESCRIPTION / UNIT / QTY / RATE /
AMOUNT table gives every figure by the column it is printed in, so a `$` before a rate, digits
split by stray spaces or a new style of item number (`a1)`) do not matter. A page with no ruled
columns falls back to being read line by line. Each page is then checked against itself: the
amounts of the items read must add up to the page's own sub-total, and each item's QTY × rate
must give its amount. A page that does not add up is named in the app, so a schedule read only
in part is said straight away rather than surfacing later as a PQ item "not in the schedule".

`check_batch()`, which checks a `data/` folder in code, reads its schedules from a `price/`
folder next to `app.py` instead. `price/` is not committed either.

## Code

Reading documents is kept apart from judging them, so a new contract needs a new reader, not
changes to the checks.

- `checker/readers.py` — turns a batch into mastersheet items plus evidence, and picks the format
- `checker/mastersheet.py`, `report.py` — the RM formats; `photo_folder_format.py` — the photo format; `bundled_pdf_format.py` — the bundle format
- `checker/photos.py` — watermark dates, AFTER photos, board dimensions
- `checker/prices.py` — reads the uploaded rate schedules (or `price/`) and picks the one a mastersheet is billed against
- `checker/checks.py` — the five checks; knows nothing about page layouts
- `checker/common.py`, `cache.py`, `parallel.py` — OCR, remembered results, worker pool
- `app.py` — the interface

**To support another contract**, write a reader returning the same two records and register it
in `readers.py`. Anything a format cannot supply is left empty, and the check that needs it
reports N/A.

## Settings

| Variable | Effect |
|---|---|
| `BILLINGOCR_WORKERS` | OCR worker processes. `1` runs in a single process, easier to debug. |
| `BILLINGOCR_CACHE` | Where remembered OCR is kept. Empty switches it off. |

The cache lives in `.ocr_cache/`, keyed by the image itself, so a replaced photo is re-read
automatically. Deleting it only costs time.

## Limits

- Photo batches assume the last photo of the day shows finished work; nothing distinguishes a
  late "during" shot.
- A board with no dimensions written on it cannot be verified at all.
- Hand-writing is read imperfectly, so a few items per batch are flagged as unreadable.
- The photo measurements are the contractor's own labels, read from the PDF text. The numbers on
  the tape itself are not read, and photo folders (TR387) have no labels to compare.
- There is no automated test suite yet; changes are checked by re-running whole batches and
  comparing against previous results.
- Only run on Linux and WSL so far. Nothing in it is platform-specific, but the Windows
  commands above and the worker pool's behaviour there are untested.
