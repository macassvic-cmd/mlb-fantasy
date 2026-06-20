"""Shared hard-timeout wrapper for blocking third-party calls (pybaseball,
requests) that can hang with no timeout of their own - one stuck call must
never block the rest of the pipeline.

Python has no safe way to forcibly kill a running thread, so a timed-out
call's thread is abandoned (daemon=True keeps it from blocking process
exit) rather than actually terminated. Each call gets its own thread
instead of a shared pool, so one stuck call can never starve out others.
"""

import logging
import threading

logger = logging.getLogger(__name__)


def call_with_timeout(fn, *args, timeout_s=60, default=None, label="", **kwargs):
    result = {}
    error = {}

    def target():
        try:
            result["value"] = fn(*args, **kwargs)
        except Exception as e:
            error["value"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)

    name = label or getattr(fn, "__name__", "call")
    if thread.is_alive():
        logger.warning(f"{name} exceeded {timeout_s}s - skipping")
        return default
    if "error" in error:
        logger.warning(f"{name} failed: {error['value']}")
        return default
    return result.get("value", default)
