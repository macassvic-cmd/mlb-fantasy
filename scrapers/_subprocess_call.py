"""Process-isolated wrapper for third-party calls that can segfault (native
code in pandas/pyarrow/pybaseball). A thread-based timeout (see _timeout.py)
cannot help here - a SIGSEGV kills the whole process, every thread with it.
Running the call in a child process means a crash only takes down that
child; the parent sees a non-zero/negative exitcode and treats it exactly
like a timed-out call: skip and move on.
"""

import logging
import multiprocessing as mp

logger = logging.getLogger(__name__)


def _run(fn, args, kwargs, queue):
    try:
        queue.put(("ok", fn(*args, **kwargs)))
    except Exception as e:
        queue.put(("error", e))


def call_in_subprocess(fn, *args, timeout_s=60, retries=1, default=None, label="", **kwargs):
    """Run fn(*args, **kwargs) in a child process.

    Returns fn's result, or `default` if the call times out, crashes
    (segfault or otherwise), or raises - after `retries` extra attempts.
    """
    name = label or getattr(fn, "__name__", "call")
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        queue = mp.Queue()
        proc = mp.Process(target=_run, args=(fn, args, kwargs, queue), daemon=True)
        proc.start()
        proc.join(timeout_s)

        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            logger.warning(
                f"{name} exceeded {timeout_s}s (attempt {attempt}/{attempts}) - "
                f"{'retrying' if attempt < attempts else 'skipping'}"
            )
            continue

        if proc.exitcode != 0:
            logger.warning(
                f"{name} crashed with exit code {proc.exitcode} "
                f"(attempt {attempt}/{attempts}) - "
                f"{'retrying' if attempt < attempts else 'skipping'}"
            )
            continue

        try:
            status, payload = queue.get_nowait()
        except Exception:
            logger.warning(
                f"{name} exited cleanly but returned no result "
                f"(attempt {attempt}/{attempts}) - "
                f"{'retrying' if attempt < attempts else 'skipping'}"
            )
            continue

        if status == "error":
            logger.warning(f"{name} raised: {payload}")
            return default

        return payload

    logger.warning(f"{name} failed after {attempts} attempt(s) - skipping")
    return default
