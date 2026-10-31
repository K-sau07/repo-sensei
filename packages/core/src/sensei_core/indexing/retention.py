"""Snapshot retention (5.2.5): which snapshots have outlived their use.

Each index run takes a snapshot of its own; an incremental one builds on the repository's last
index, and the snapshots before it serve diffs and older links for a while. A snapshot taken
more than ``ingest.snapshot_retention_days`` ago goes, unless it is still in use: the
repository's index (its last indexed commit), what a ref reads (``repo_refs``: a branch's or a
pull request's last good run, not merely its latest), or one a run queued or going is
building.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import ACTIVE_JOB_STATUSES, IndexJob, Repo, RepoRef, Snapshot

SWEEP_LIMIT = 200  # snapshots deleted per sweep (the next one takes the rest)


async def expired(
    db: AsyncSession, *, before: datetime, limit: int = SWEEP_LIMIT
) -> list[Snapshot]:
    """Snapshots taken before ``before`` and in no use (see the module), oldest first. Reads
    every org's: a system session."""
    pointed = exists().where(RepoRef.snapshot_id == Snapshot.id)  # what a ref reads (5.4)
    building = exists().where(
        IndexJob.snapshot_id == Snapshot.id, IndexJob.status.in_(ACTIVE_JOB_STATUSES)
    )
    found = await db.scalars(
        select(Snapshot)
        .join(Repo, Repo.id == Snapshot.repo_id)
        .where(
            Snapshot.created_at < before,
            Snapshot.commit_sha != func.coalesce(Repo.last_indexed_sha, ""),
            ~pointed,
            ~building,
        )
        .order_by(Snapshot.created_at)
        .limit(limit)
    )
    return list(found)
