"""
Remember what OCR read, so the same image is never read twice.

OCR is the slow part of a run (about a second per photo) and the photos
never change, so a second run over the same batch should not repeat it.
Entries are keyed by the image's own pixels: edit or replace a photo and
it is read again, with no way to serve a stale result.

One small file per entry, written atomically, so the eight worker
processes can fill the cache at once without locking or corruption.
"""
import hashlib
import os
import tempfile

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".ocr_cache")


def cache_dir():
    """Where entries live. BILLINGOCR_CACHE overrides; empty disables the cache."""
    if "BILLINGOCR_CACHE" in os.environ:
        return os.environ["BILLINGOCR_CACHE"] or None
    return DEFAULT_DIR


def key_for(image):
    """A key from the pixels themselves, so any change to the image misses."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(f"{image.mode} {image.width}x{image.height}|".encode())
    digest.update(image.tobytes())
    return digest.hexdigest()


def _path(directory, key):
    # Two-character prefix keeps the directory from growing unwieldy.
    return os.path.join(directory, key[:2], key[2:] + ".txt")


def get(key):
    directory = cache_dir()
    if not directory:
        return None
    try:
        with open(_path(directory, key), encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def put(key, text):
    directory = cache_dir()
    if not directory:
        return
    path = _path(directory, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write elsewhere then rename: a reader never sees a half-written
        # entry, and two workers writing the same key cannot interleave.
        fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp, path)
    except OSError:
        pass  # a cache that cannot be written is not a reason to fail the run
