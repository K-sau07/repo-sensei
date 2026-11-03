"""What an index run's end tells the org's admins (8.10):

- a repository's **first** index done (later ones, a push's, are routine);
- a run that **failed**, unless the repository's run before it failed too (a string of failures
  says so once, until one succeeds).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import IndexJob, JobStatus, NotificationKind
from sensei_core.notifications.service import Event, Told, admins, notify, repo_place


async def index_notice(db: AsyncSession, job: IndexJob) -> Told | None:
    """Tell the admins of ``job``'s end (it has just succeeded or failed, in ``db``'s
    transaction), if it is news."""
    if job.status not in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        return None
    place = await repo_place(db, job.org_id, job.repo_id)
    if place is None:
        return None
    if job.status is JobStatus.SUCCEEDED:
        ever = await db.scalar(
            select(IndexJob.id)
            .where(
                IndexJob.org_id == job.org_id,
                IndexJob.repo_id == job.repo_id,
                IndexJob.id != job.id,
                IndexJob.status == JobStatus.SUCCEEDED,
            )
            .limit(1)
        )
        if ever is not None:
            return None
        event = Event(
            job.org_id, NotificationKind.INDEX_DONE, f"repo:{job.repo_id}:first-index",
            f"{place.full_name} is indexed",
            "Its code browser, search and Ask are ready.", place.path, repo_id=job.repo_id,
        )  # fmt: skip
    else:
        before = await db.scalar(
            select(IndexJob.status)
            .where(
                IndexJob.org_id == job.org_id,
                IndexJob.repo_id == job.repo_id,
                IndexJob.id != job.id,
                IndexJob.status.in_((JobStatus.SUCCEEDED, JobStatus.FAILED)),
            )
            .order_by(IndexJob.finished_at.desc().nulls_last(), IndexJob.created_at.desc())
            .limit(1)
        )
        if before is JobStatus.FAILED:  # said already, at the first of these failures
            return None
        error = job.error or {}
        message = error.get("message") if isinstance(error.get("message"), str) else None
        event = Event(
            job.org_id, NotificationKind.INDEX_FAILED, f"index:{job.id}:failed",
            f"Indexing {place.full_name} failed", message or "The run stopped with an error.",
            f"{place.path}/index", repo_id=job.repo_id,
        )  # fmt: skip
    return await notify(db, event, await admins(db, job.org_id))
