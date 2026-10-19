"""A worker app for the redelivery test: the real configuration plus one task that hangs on its
first attempt (so the test can kill the process running it)."""

from __future__ import annotations

import os
import time

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
