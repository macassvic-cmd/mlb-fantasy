"""Process-isolated wrapper for third-party calls that can segfault (native
code in pandas/pyarrow/pybaseball). A thread-based timeout (see _timeout.py)
cannot help here - a SIGSEGV kills the whole process, every thread with it.

WorkerPool keeps ONE persistent child process alive across many calls,
rather than spawning a fresh interpreter per call. A crash or hang in the
child only costs that one call (skip, same contract as a timed-out fetch);
the pool respawns a replacement worker and keeps going. This matters
because "spawn" (required - see below) costs 1-2s of interpreter startup
each time, which is fine paid once but not 273 times over a per-player loop.
"""

import logging
import multiprocessing as mp

logger = logging.getLogger(__name__)

# Force "spawn" rather than the platform default. On Linux, mp's default is
# "fork", which clones the parent's entire memory image - including any
# lock currently held by another thread. This pipeline runs dozens of
# background threads before we get here (call_with_timeout spawns one per
# call and *abandons* it on timeout per its own docstring; ThreadPoolExecutor
# runs 8 workers for platoon splits). If fork() lands while one of those
# threads holds a native lock (requests/urllib3/SSL), the forked child
# inherits it already locked forever and hangs on its first request - which
# is exactly what turned "one call segfaults" into "every call times out"
# when a fresh fork-per-call wrapper first went live. spawn starts a fresh
# interpreter with no inherited threads or locks, so that hazard doesn't
# apply - but its startup cost must be paid once per pool, not once per call.
_ctx = mp.get_context("spawn")


def _worker_loop(fn, task_q, result_q):
    while True:
        item = task_q.get()
        if item is None:
            return
        args, kwargs = item
        try:
            result_q.put(("ok", fn(*args, **kwargs)))
        except Exception as e:
            result_q.put(("error", e))


class WorkerPool:
    """Runs `fn` in one persistent child process, reused across calls.

    Restarts the child only when it actually dies (crash or a call that
    exceeded timeout_s and had to be killed) - not on every call.
    """

    def __init__(self, fn, timeout_s=60, retries=1, default=None, label=""):
        self.fn = fn
        self.timeout_s = timeout_s
        self.retries = retries
        self.default = default
        self.label = label or getattr(fn, "__name__", "call")
        self.task_q = None
        self.result_q = None
        self.proc = None

    def _ensure_worker(self):
        if self.proc is not None and self.proc.is_alive():
            return
        self.task_q = _ctx.Queue()
        self.result_q = _ctx.Queue()
        self.proc = _ctx.Process(
            target=_worker_loop, args=(self.fn, self.task_q, self.result_q), daemon=True,
        )
        self.proc.start()

    def _kill_worker(self):
        if self.proc is not None:
            if self.proc.is_alive():
                self.proc.terminate()
                self.proc.join(5)
            self.proc = None

    def call(self, *args, **kwargs):
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            self._ensure_worker()
            self.task_q.put((args, kwargs))
            try:
                status, payload = self.result_q.get(timeout=self.timeout_s)
            except Exception:
                alive = self.proc.is_alive()
                self._kill_worker()
                reason = "hung" if alive else "crashed"
                logger.warning(
                    f"{self.label} {reason} (attempt {attempt}/{attempts}) - "
                    f"{'retrying on a fresh worker' if attempt < attempts else 'skipping'}"
                )
                continue

            if status == "error":
                logger.warning(f"{self.label} raised: {payload}")
                return self.default

            return payload

        logger.warning(f"{self.label} failed after {attempts} attempt(s) - skipping")
        return self.default
