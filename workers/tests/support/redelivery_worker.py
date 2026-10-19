"""A worker app for the redelivery tests: the real configuration plus tasks that misbehave on
purpose (hang, die, ignore the soft time limit)."""

from __future__ import annotations

import contextlib
import os
import signal
import time

from celery.exceptions import SoftTimeLimitExceeded

from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import get_runtime

__all__ = ["celery_app"]


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name="sensei.default.test_hang_once",
    idempotent_on=("job_id",),
)
def hang_once(self: SenseiTask, *, job_id: str, marker: str) -> str:
    redis = get_runtime().sync_redis
    attempt = int(redis.incr(f"{marker}:attempts"))  # pyright: ignore[reportArgumentType]
    redis.set(f"{marker}:pid:{attempt}", os.getpid())
    if attempt == 1:
        time.sleep(120)  # the test kills this process here
    redis.incr(f"{marker}:completions")
    return f"done {job_id}"


@celery_app.task(bind=True, base=SenseiTask, name="sensei.default.test_always_dies")
def always_dies(self: SenseiTask, *, marker: str) -> None:
    """Its process dies on every delivery (think: out of memory)."""
    get_runtime().sync_redis.incr(f"{marker}:deliveries")
    os.kill(os.getpid(), signal.SIGKILL)


@celery_app.task(bind=True, base=SenseiTask, name="sensei.default.test_ignores_soft_limit")
def ignores_soft_limit(self: SenseiTask, *, marker: str) -> None:
    """Swallows the soft time limit, so only the hard limit stops it."""
    get_runtime().sync_redis.set(f"{marker}:started", "1")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with contextlib.suppress(SoftTimeLimitExceeded):
            time.sleep(0.1)
