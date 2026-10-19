"""Repository maintenance (system tasks: they run outside any request and its org context)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete

from sensei_core.db import rowcount
from sensei_core.jobs import TaskName
from sensei_core.models import Repo
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import get_runtime


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name=TaskName.PURGE_REPO.value,
    system=True,
    requires=("repo_id",),
    # No idempotency lock: deleting a deleted row is already a no-op, and a "done" marker would
    # skip the purge after a later disconnect of the same repository.
)
def purge_repo(self: SenseiTask, *, repo_id: str) -> bool:
    """Remove a disconnected repository for good. Only one that is still disconnected: if it was
    connected again in the meantime, it stays."""

    async def _purge() -> bool:
        async with self.system_session() as db:
            result = await db.execute(
                delete(Repo).where(Repo.id == uuid.UUID(repo_id), Repo.deleted_at.is_not(None))
            )
            return rowcount(result) == 1

    return get_runtime().run(_purge())
