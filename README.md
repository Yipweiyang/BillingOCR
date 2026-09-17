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

Then pick a batch from `data/`, or upload a mastersheet and its report PDFs.

The first run over new photos takes a few minutes, because every image is read by OCR. The
results are remembered, so running the same batch again takes seconds.

## Data layout

One folder per batch, holding the mastersheet and its evidence. **The mastersheet PDF must be
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

Results are **PASS**, **FLAG** (the evidence contradicts the mastersheet), **REVIEW** (it could
not be read well enough to decide — usually hand-writing, so it needs eyes, not suspicion),
**N/A** (this format has nothing to check here) or **NOT RUN** (check 1 found no single piece
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

## Code

Reading documents is kept apart from judging them, so a new contract needs a new reader, not
changes to the checks.

- `checker/readers.py` — turns a batch into mastersheet items plus evidence, and picks the format
- `checker/mastersheet.py`, `report.py` — the RM formats; `tr387.py` — the photo format; `tr388.py` — the bundle format
- `checker/photos.py` — watermark dates, AFTER photos, board dimensions
- `checker/checks.py` — the four checks; knows nothing about page layouts
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
- Hand-writing is read imperfectly, so a few items per batch come back as REVIEW.
- There is no automated test suite yet; changes are checked by re-running whole batches and
  comparing against previous results.
- Only run on Linux and WSL so far. Nothing in it is platform-specific, but the Windows
  commands above and the worker pool's behaviour there are untested.
