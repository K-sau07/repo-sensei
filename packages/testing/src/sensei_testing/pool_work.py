"""Work functions for process-pool tests: worker processes import them by name, which test
modules (loaded with ``--import-mode=importlib``) can't offer."""

from __future__ import annotations

import multiprocessing
import os
import signal
import threading
import time
from typing import Any


def shout(item: str) -> str:
    """``"crash"`` kills the worker process, ``"raise"`` raises; anything else comes back
    upper-cased."""
    if item == "crash":
        os.kill(os.getpid(), signal.SIGKILL)
    if item == "raise":
        raise ValueError("boom")
    return item.upper()


def nap(seconds: float) -> float:
    time.sleep(seconds)
    return seconds


def pool_in_daemon(queue: Any) -> None:
    """Run in a billiard daemon process (what a Celery prefork worker is): a pool's results."""
    from sensei_engine.parse.pool import run_parallel  # noqa: PLC0415 - in the child only

    try:
        queue.put(sorted((k, repr(v)) for k, v in run_parallel(shout, ["a", "crash"], workers=2)))
    except Exception as exc:  # reported to the test, which fails on it
        queue.put(repr(exc))


def pool_then_wait(queue: Any) -> None:
    """Start a pool on slow work and report its worker pids (the test then kills this process)."""
    from sensei_engine.parse.pool import run_parallel  # noqa: PLC0415 - in the child only

    def report() -> None:
        time.sleep(1.5)
        queue.put([child.pid for child in multiprocessing.active_children()])

    threading.Thread(target=report, daemon=True).start()
    list(run_parallel(nap, [30.0, 30.0], workers=2, batch_size=1))
