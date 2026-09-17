"""
Batch readers: turn one batch's files into master items and evidence.

Every reader returns (items, evidence) in the same shape, so the checks
never depend on a contract's document layout:

    items:    {key: {"sn", "date", "jobs": [{"pq", "length", "width", "qty"}]}}
              TR387 items also carry "location", "landmark" and "areas"
    evidence: [{"key", "source", "claimed_jobs", "sketch_jobs", "after_photos", "oic"}]

    claimed_jobs  [{"pq", "qty"}] the contractor bills, or None if unreadable
    sketch_jobs   [{"pq", "length", "width", "qty"}] from the sketch
    board_dims    [{"length", "width", "label"}] hand-written on site boards (TR387 only)
    board_texts   [{"label", "text", "path"}] raw OCR of those photos (TR387 only)
    sketch_errors [str] sketch arithmetic that does not add up (TR388 only)
    after_photos  [{"label", "image" or "path", "group", "text"}] - text is the
                  OCR already done by the reader, group splits a shared folder
    oic           {"found", "detail"}

A field the format cannot provide at all is None, and the check that
needs it reports N/A instead of a FLAG.

Readers do all the OCR, in parallel, so that the checks are cheap and a
caller can show progress while the slow part runs.
"""
import os

from .mastersheet import parse_master
from .parallel import pmap
from .report import parse_report
from .tr387 import photo_folders, read_tr387_batch
from .tr388 import is_tr388_bundle, is_tr388_master, read_tr388_batch


def _read_one_report(job):
    """One incident report, in a worker process."""
    filename, data, expected_refs = job
    return parse_report(data, filename, expected_refs)


def read_rm_pdfs(master_bytes, reports, progress=None):
    """
    RM205/RM206: one mastersheet PDF plus one incident report PDF per
    defect, keyed by defect reference. reports is [(filename, bytes)].
    """
    items = parse_master(master_bytes)
    refs = list(items)
    jobs = [(name, data, refs) for name, data in reports]
    evidence = pmap(_read_one_report, jobs, progress=progress)
    return items, evidence


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def read_batch(batch_dir, progress=None):
    """
    A batch folder holds the mastersheet PDF, named after the folder, and
    its evidence. The format is recognised from what else is in the folder.
    Returns (items, evidence, what one piece of evidence is called).
    """
    name = os.path.basename(os.path.normpath(batch_dir))
    master = os.path.join(batch_dir, name + ".pdf")
    if not os.path.isfile(master):
        raise ValueError(f"No mastersheet found: expected '{name}.pdf' inside the batch folder")

    report_pdfs = sorted(f for f in os.listdir(batch_dir)
                         if f.lower().endswith(".pdf") and f != name + ".pdf")
    if report_pdfs:
        reports = [(f, _read(os.path.join(batch_dir, f))) for f in report_pdfs]
        master_bytes = _read(master)
        # TR388: one bundle PDF holds a whole sector's incidents.
        if is_tr388_master(master_bytes) and all(is_tr388_bundle(data) for _, data in reports):
            items, evidence = read_tr388_batch(master_bytes, reports, progress=progress)
            return items, evidence, "incident report"
        items, evidence = read_rm_pdfs(master_bytes, reports, progress=progress)
        return items, evidence, "incident report"

    if photo_folders(batch_dir):
        items, evidence = read_tr387_batch(batch_dir, _read(master), progress=progress)
        return items, evidence, "photo folder"

    raise ValueError(f"Unsupported batch format in '{name}': no incident report PDFs "
                     f"and no photo folders named by S/N")
