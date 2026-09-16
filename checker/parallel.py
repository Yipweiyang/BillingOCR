"""
Spread the OCR work over processes.

OCR is CPU-bound and holds the GIL, so threads buy nothing; processes are
the only way to use more than one core. Set BILLINGOCR_WORKERS to override
the pool size (1 disables parallelism, which is useful when debugging).
"""
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

# Each worker loads its own OCR model, so more workers cost memory as well
# as cores. Eight is a reasonable ceiling on a typical machine.
MAX_WORKERS = 8

def _single_threaded():
    """
    Worker start-up, before the OCR model is loaded.

    Left alone, every worker spreads each image over all cores and eight of
    them thrash the machine - slower than running serially (measured: 4m33s
    wall for 66m of CPU). Thread environment variables do not reach the OCR
    runtime; only its own session option does, which is what this sets.
    """
    from .common import use_single_thread

    use_single_thread()


# forkserver first: workers are forked from a small, clean server process,
# so they inherit none of the OCR runtime (a worker forked from a process
# that has already loaded it dies at once) and the caller's main module is
# never re-imported (which spawn does, re-running whatever script started
# the batch - a Streamlit app included). spawn is the fallback where
# forkserver does not exist, such as native Windows.
START_METHODS = ("forkserver", "spawn")


def _start_pool(count):
    """A pool that is safe to start even from a process that has used OCR."""
    for method in START_METHODS:
        try:
            context = multiprocessing.get_context(method)
        except ValueError:
            continue
        try:
            return ProcessPoolExecutor(max_workers=count, initializer=_single_threaded,
                                       mp_context=context)
        except (OSError, ValueError):
            continue
    return None  # no processes available here; the caller falls back to serial


def worker_count(n_items):
    override = os.environ.get("BILLINGOCR_WORKERS", "")
    if override.isdigit():
        return max(1, min(int(override), n_items))
    return max(1, min(os.cpu_count() or 2, MAX_WORKERS, n_items))


def pmap(fn, items, progress=None, workers=None):
    """
    [fn(item) for item in items], in order, computed in parallel.

    progress(done, total) is called as results land, so a caller can draw a
    progress bar. fn must be importable by name (a module-level function),
    because each item is pickled to a worker process.
    """
    items = list(items)
    if not items:
        return []

    count = workers if workers is not None else worker_count(len(items))
    pool = _start_pool(count) if count > 1 else None

    if pool is None:
        out = []
        for done, item in enumerate(items, 1):
            out.append(fn(item))
            if progress:
                progress(done, len(items))
        return out

    with pool:
        out = [None] * len(items)
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for done, future in enumerate(as_completed(futures), 1):
            out[futures[future]] = future.result()
            if progress:
                progress(done, len(items))
        return out
