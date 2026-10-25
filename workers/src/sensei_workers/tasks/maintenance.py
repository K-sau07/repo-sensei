"""Vacuuming a partition of ``chunks`` (see :mod:`sensei_workers.statistics`), apart from the
stages that delete rows.

:func:`request_vacuum` asks for the repository's partition to be vacuumed in
:data:`DEBOUNCE_SECONDS`; requests for the same partition meanwhile (another repository hashed
there, or the same job's finalize) are folded into that one (a Redis key, set if absent). The
task clears the key as it starts, so rows deleted while it runs get a vacuum of their own. A
VACUUM that can't have its lock in time is tried again later.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import DBAPIError

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_workers.app import celery_app
from sensei_workers.base import RetryableError, SenseiTask
from sensei_workers.runtime import get_runtime
from sensei_workers.statistics import chunk_partition_of, vacuum_partition

log = get_logger(__name__)

DEBOUNCE_SECONDS = 30
KEY_TTL_SECONDS = 3600  # a lost task doesn't hold off vacuums for longer


LOCK_NOT_AVAILABLE = "55P03"


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


def vacuum_key(partition: str) -> str:
    return f"sensei:vacuum:{partition}"


def request_vacuum(repo_id: uuid.UUID, *, eager: bool = False) -> bool:
    """Have the repository's partition vacuumed soon (at once, in place, when ``eager``: a
    task run in place asks); False if a vacuum of it is already waiting. Never raises
    (autovacuum gets there in the end)."""
    runtime = get_runtime()
    try:
        partition = runtime.run(chunk_partition_of(repo_id))
        if not runtime.sync_redis.set(vacuum_key(partition), "1", nx=True, ex=KEY_TTL_SECONDS):
            return False
        if eager:
            vacuum_chunks.apply(kwargs={"partition": partition})
        else:
            vacuum_chunks.apply_async(kwargs={"partition": partition}, countdown=DEBOUNCE_SECONDS)
    except Exception:
        log.warning("couldn't ask for a vacuum", repo_id=str(repo_id), exc_info=True)
        return False
    return True


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.VACUUM_CHUNKS.value, system=True)
def vacuum_chunks(self: SenseiTask, *, partition: str) -> str:
    """VACUUM (ANALYZE) one partition of ``chunks``."""
    runtime = get_runtime()
    runtime.sync_redis.delete(vacuum_key(partition))
    try:
        runtime.run(vacuum_partition(partition))
    except DBAPIError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE:
            raise RetryableError("the partition is locked") from exc
        raise
    log.info("chunks vacuumed", partition=partition)
    return partition
