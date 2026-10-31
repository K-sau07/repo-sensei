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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from sensei_core.indexing import retention
from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, IndexJob, Snapshot
from sensei_core.storage import Bucket, repo_prefix, snapshot_key
from sensei_workers.app import celery_app
from sensei_workers.base import RetryableError, SenseiTask
from sensei_workers.runtime import get_runtime
from sensei_workers.statistics import chunk_partition_of, vacuum_partition

log = get_logger(__name__)

DEBOUNCE_SECONDS = 30
KEY_TTL_SECONDS = 3600  # a lost task doesn't hold off vacuums for longer


LOCK_NOT_AVAILABLE = "55P03"


def sqlstate(exc: DBAPIError) -> str | None:
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
        key = vacuum_key(partition)
        if not runtime.sync_redis.set(key, "1", nx=True, ex=KEY_TTL_SECONDS):
            return False
        try:
            if eager:
                vacuum_chunks.apply(kwargs={"partition": partition})
            else:
                vacuum_chunks.apply_async(
                    kwargs={"partition": partition}, countdown=DEBOUNCE_SECONDS
                )
        except Exception:
            runtime.sync_redis.delete(key)  # not sent: the next request sends one
            raise
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
        if sqlstate(exc) == LOCK_NOT_AVAILABLE:
            raise RetryableError("the partition is locked") from exc
        raise
    log.info("chunks vacuumed", partition=partition)
    return partition


@dataclass(frozen=True, slots=True)
class _Gone:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    commit_sha: str


async def _expire(before: datetime) -> list[_Gone]:
    """Delete the snapshots past their retention (their files, symbols, chunks and edges go
    with them); what each was, for its objects."""
    async with get_runtime().system_database.session() as db:
        found = await retention.expired(db, before=before)
        gone = [_Gone(s.org_id, s.repo_id, s.commit_sha) for s in found]
        for snapshot in found:
            await db.delete(snapshot)
        return gone


async def _drop_objects(gone: _Gone) -> None:
    """A deleted snapshot's archive and artifacts (best effort: a repository's purge takes
    whatever is left)."""
    storage = await get_runtime().storage()
    await storage.delete_prefix(
        Bucket.SNAPSHOTS, snapshot_key(gone.org_id, gone.repo_id, gone.commit_sha)
    )
    prefix = repo_prefix(Bucket.ARTIFACTS, gone.org_id, gone.repo_id)
    await storage.delete_prefix(Bucket.ARTIFACTS, f"{prefix}{gone.commit_sha}/")


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.SWEEP_SNAPSHOTS.value, system=True)
def sweep_snapshots(self: SenseiTask) -> int:
    """Delete the snapshots past ``ingest.snapshot_retention_days``; returns how many went."""
    runtime = get_runtime()
    days = runtime.settings.ingest.snapshot_retention_days
    gone = runtime.run(_expire(datetime.now(UTC) - timedelta(days=days)))
    for one in gone:
        try:
            runtime.run(_drop_objects(one))
        except Exception:
            log.warning(
                "couldn't delete a snapshot's objects", repo_id=str(one.repo_id), exc_info=True
            )
    if gone:
        log.info("snapshots expired", snapshots=len(gone))
    return len(gone)


DROP_WAIT_SECONDS = 60  # a ref whose run is going is dropped once it ends


async def _drop_ref(
    task: SenseiTask, org_id: uuid.UUID, repo_id: uuid.UUID, ref: str
) -> list[_Gone] | None:
    """Delete a ref's snapshots (a closed pull request's overlay, 5.4); None while a run of it
    is queued or going (dropped once it ends)."""
    async with task.org_session(org_id) as db:
        building = await db.scalar(
            select(IndexJob.id).where(
                IndexJob.repo_id == repo_id,
                IndexJob.ref == ref,
                IndexJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if building is not None:
            return None
        found = list(
            await db.scalars(
                select(Snapshot).where(Snapshot.repo_id == repo_id, Snapshot.ref == ref)
            )
        )
        gone = [_Gone(s.org_id, s.repo_id, s.commit_sha) for s in found]
        for snapshot in found:
            await db.delete(snapshot)
        return gone


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name=TaskName.DROP_REF.value,
    requires=("org_id", "repo_id", "ref"),
)
def drop_ref(self: SenseiTask, *, org_id: str, repo_id: str, ref: str) -> int:
    """Drop a closed pull request's overlay: its snapshots, archives and artifacts."""
    runtime = get_runtime()
    gone = runtime.run(_drop_ref(self, uuid.UUID(org_id), uuid.UUID(repo_id), ref))
    if gone is None:
        drop_ref.apply_async(
            kwargs={"org_id": org_id, "repo_id": repo_id, "ref": ref}, countdown=DROP_WAIT_SECONDS
        )
        return 0
    for one in gone:
        try:
            runtime.run(_drop_objects(one))
        except Exception:
            log.warning("couldn't delete a snapshot's objects", ref=ref, exc_info=True)
    log.info("ref dropped", ref=ref, snapshots=len(gone))
    return len(gone)
