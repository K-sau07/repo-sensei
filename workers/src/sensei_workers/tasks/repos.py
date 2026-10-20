"""Repository maintenance (system tasks: they run outside any request and its org context)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete

from sensei_core.db import rowcount
from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import Repo
from sensei_core.storage import Bucket, repo_prefix
from sensei_workers.app import celery_app
from sensei_workers.base import RetryableError, SenseiTask
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name=TaskName.PURGE_REPO.value,
    system=True,
    requires=("org_id", "repo_id"),
    # No idempotency lock: purging twice is harmless, and a "done" marker would skip the purge
    # after a later disconnect of the same repository.
)
def purge_repo(self: SenseiTask, *, org_id: str, repo_id: str) -> bool:
    """Remove a disconnected repository for good: its stored snapshots and artifacts, then its
    row (snapshots, index jobs and files go with it). One connected again in the meantime stays.
    Returns whether there was anything to purge."""
    runtime = get_runtime()
    org, repo = uuid.UUID(org_id), uuid.UUID(repo_id)

    async def _disconnected() -> bool | None:
        """True: disconnected; False: connected; None: already gone."""
        async with self.system_session() as db:
            row = await db.get(Repo, repo)
            if row is None:
                return None
            return row.org_id == org and row.deleted_at is not None

    async def _objects() -> None:
        storage = await runtime.storage()
        for bucket in Bucket:
            await storage.delete_prefix(bucket, repo_prefix(bucket, org, repo))

    async def _row() -> bool:
        async with self.system_session() as db:
            result = await db.execute(
                delete(Repo).where(Repo.id == repo, Repo.deleted_at.is_not(None))
            )
            return rowcount(result) == 1

    state = runtime.run(_disconnected())
    if state is False:
        return False
    try:
        runtime.run(_objects())  # first, so a failure leaves the row to try again from
    except Exception as exc:
        raise RetryableError("object storage unavailable") from exc
    purged = runtime.run(_row())
    log.info("repository purged", repo_id=repo_id, row=purged)
    return True
