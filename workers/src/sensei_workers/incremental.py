"""What an incremental run's stages keep of the last index (5.2, :mod:`sensei_engine.incremental`):
worked out for the stage that asks, from the job, its snapshot and the repository's last index."""

from __future__ import annotations

import uuid

from sensei_core.models import IndexJob, JobKind, Repo, Snapshot
from sensei_engine.incremental import Carry, carried, previous_of
from sensei_workers.base import SenseiTask


async def carry_for(
    task: SenseiTask, org_id: uuid.UUID, job_id: uuid.UUID, snapshot_id: uuid.UUID
) -> Carry | None:
    """What the job's snapshot keeps of the last index; None for a full run, or when there is
    no last index to build on (the run then reads everything)."""
    async with task.org_session(org_id) as db:
        job = await db.get(IndexJob, job_id)
        if job is None or job.kind is not JobKind.INCREMENTAL:
            return None
        repo = await db.get(Repo, job.repo_id)
        snapshot = await db.get(Snapshot, snapshot_id)
        if repo is None or snapshot is None:
            return None
        previous = await previous_of(db, repo, snapshot)
        return await carried(db, previous, snapshot) if previous is not None else None
