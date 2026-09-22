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
    sketch_sizes  [{"length", "width"}] every L x W in the sketch (TR388 only)
    photo_dims    [{"label", "values"}] distances typed over tape photos and the
                  box drawn round the repair, in metres (PDF formats only)
    after_photos  [{"label", "image" or "path", "group", "text"}] - text is the
                  OCR already done by the reader, group splits a shared folder
    oic           {"found", "detail"}

A field the format cannot provide at all is None, and the check that
needs it reports N/A instead of a FLAG.

Readers do all the OCR, in parallel, so that the checks are cheap and a
caller can show progress while the slow part runs.
"""
import atexit
import hashlib
import io
import os
import shutil
import tempfile
import zipfile

from .bundled_pdf_format import is_tr388_bundle, is_tr388_master, read_tr388_batch
from .mastersheet import is_rm_master, parse_master
from .parallel import pmap
from .photo_folder_format import is_tr387_master, photo_folders, read_tr387_batch
from .report import parse_report


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


def _is_pdf(data):
    return data[:5] == b"%PDF-"


def _photo_root(directory):
    """
    The directory the S/N folders sit in. A zip may hold them at its root
    or inside one folder named after the batch.
    """
    if photo_folders(directory):
        return directory
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isdir(path) and name != "__MACOSX" and photo_folders(path):
            return path
    return None


_UPLOAD_ROOT = None


def _upload_root():
    """
    Where uploaded photos are unpacked, for as long as the app runs.

    The checks re-open photos by path - a board is read again, larger, when
    nothing else settles it - so the files have to outlive the reading, not
    just the unpacking. The directory goes when the process does.
    """
    global _UPLOAD_ROOT
    if _UPLOAD_ROOT is None:
        _UPLOAD_ROOT = tempfile.mkdtemp(prefix="billingocr-")
        atexit.register(shutil.rmtree, _UPLOAD_ROOT, True)
    return _UPLOAD_ROOT


def _read_photo_zips(zips, master_bytes, progress):
    """Unpack uploaded zips of S/N folders and read them as one batch."""
    # Named after the bytes, so uploading the same zip twice unpacks once.
    digest = hashlib.blake2b(digest_size=16)
    for name, data in sorted(zips):
        digest.update(data)
    unpacked = os.path.join(_upload_root(), digest.hexdigest())

    if not os.path.isdir(unpacked):
        staging = unpacked + ".part"
        shutil.rmtree(staging, ignore_errors=True)
        for name, data in zips:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    # extractall drops leading separators and ".." itself,
                    # so nothing can be written outside the staging directory.
                    z.extractall(staging)
            except zipfile.BadZipFile:
                shutil.rmtree(staging, ignore_errors=True)
                raise ValueError(f"{name} is not a readable ZIP file.")
        # Renamed only once complete, so a half-unpacked zip is never reused.
        os.replace(staging, unpacked)

    root = _photo_root(unpacked)
    if root is None:
        raise ValueError(
            "No photo folders found in the ZIP. Each folder is named by S/N, "
            "for example '05-06. SERANGOON ROAD LP 71'.")
    return read_tr387_batch(root, master_bytes, progress=progress)


def read_uploads(master_bytes, reports, progress=None):
    """
    An uploaded batch: the mastersheet plus its evidence - one report PDF
    per defect, a bundle PDF per sector, or a ZIP of photo folders. The format
    is recognised from the mastersheet itself, so a batch of the wrong
    shape is refused up front rather than read as the wrong format.
    Returns (items, evidence, what one piece of evidence is called).
    """
    if is_tr388_master(master_bytes):
        # Every incident of a sector lives in one bundle PDF.
        loose = [name for name, data in reports if not _is_pdf(data) or not is_tr388_bundle(data)]
        if loose:
            raise ValueError(
                "This mastersheet is the bundled-PDF format (TR388), but these files are not "
                f"bundles: {', '.join(loose)}. Upload the bundle PDF for each sector.")
        items, evidence = read_tr388_batch(master_bytes, reports, progress=progress)
        return items, evidence, "incident report"

    # Tested before the RM sheet: this format's mastersheet carries a defect
    # reference column too (always "Whatsapp"), so the RM test would claim it.
    if is_tr387_master(master_bytes):
        # The evidence is a folder of site photos per S/N. A browser cannot
        # upload a folder, so it arrives zipped.
        zips = [(name, data) for name, data in reports if name.lower().endswith(".zip")]
        if not zips:
            raise ValueError(
                "This mastersheet is the photo-folder format (TR387), whose evidence is a folder "
                "of site photos per S/N. Upload those folders as a ZIP file.")
        items, evidence = _read_photo_zips(zips, master_bytes, progress)
        return items, evidence, "photo folder"

    if is_rm_master(master_bytes):
        # One incident report per defect. A bundle here means the two
        # formats have been mixed up; a report with no text layer is just a
        # scan, so only the positive bundle test can be trusted.
        bundles = [name for name, data in reports if _is_pdf(data) and is_tr388_bundle(data)]
        not_pdfs = [name for name, data in reports if not _is_pdf(data)]
        if not_pdfs:
            raise ValueError(
                "This mastersheet is the RM205/RM206 format, which expects one incident report "
                f"PDF per defect, but these files are not PDFs: {', '.join(not_pdfs)}.")
        if bundles:
            raise ValueError(
                "This mastersheet is the RM205/RM206 format, which expects one incident report "
                f"per defect, but these files are bundles: {', '.join(bundles)}.")
        items, evidence = read_rm_pdfs(master_bytes, reports, progress=progress)
        return items, evidence, "incident report"

    raise ValueError(
        "Unrecognised mastersheet: its table header matches none of the known formats. "
        "Check that the first file is the mastersheet and not an incident report.")


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
        return read_uploads(_read(master), reports, progress=progress)

    if photo_folders(batch_dir):
        items, evidence = read_tr387_batch(batch_dir, _read(master), progress=progress)
        return items, evidence, "photo folder"

    raise ValueError(f"Unsupported batch format in '{name}': no incident report PDFs "
                     f"and no photo folders named by S/N")
