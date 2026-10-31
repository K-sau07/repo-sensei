"""Which indexed snapshot a ``ref`` means: the last index, a commit (or its prefix), a branch."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    IndexJob,
    JobStatus,
    Repo,
    Snapshot,
    SnapshotStatus,
)

_SHA_PREFIX = re.compile(r"[0-9a-f]{7,64}")
MAX_REF_LENGTH = 255


def _not_indexed(ref: str | None) -> NotFoundError:
    if ref is None:
        return NotFoundError("This repository hasn't been indexed yet.", code="repo_not_indexed")
    return NotFoundError(f"Nothing indexed at {ref!r}.", code="ref_not_indexed")


async def resolve_snapshot(db: AsyncSession, repo: Repo, ref: str | None) -> Snapshot:
    """The browsable snapshot for ``ref``: None means the last index; a hex string of 7 or more
    characters is a commit id (or the start of one); anything else is a branch, whose most
    recently indexed commit is used. Only snapshots an index run finished count (one left by a
    failed or cancelled run may have its tree, but was never indexed)."""
    finished = (
        select(IndexJob.id)
        .where(
            IndexJob.repo_id == repo.id,
            IndexJob.snapshot_id == Snapshot.id,
            IndexJob.status == JobStatus.SUCCEEDED,
        )
        .exists()
    )
    browsable = select(Snapshot).where(
        Snapshot.repo_id == repo.id,
        Snapshot.status == SnapshotStatus.READY,
        Snapshot.merkle_root.is_not(None),
        finished,
    )
    if ref is None:
        if repo.last_indexed_sha is None:
            # Never indexed yet: the first run's snapshot, as soon as its discovery is done (the
            # code browser unlocks then; the run hasn't finished, so nothing else is indexed).
            first = await _first_run_snapshot(db, repo)
            if first is None:
                raise _not_indexed(None)
            return first
        found = await db.scalar(browsable.where(Snapshot.commit_sha == repo.last_indexed_sha))
        if found is None:
            raise _not_indexed(None)
        return found
    if len(ref) > MAX_REF_LENGTH or not ref.strip():
        raise InvalidError("That isn't a branch or commit.", code="invalid_ref")
    if _SHA_PREFIX.fullmatch(ref):
        matches = list(
            await db.scalars(browsable.where(Snapshot.commit_sha.startswith(ref)).limit(2))
        )
        if len(matches) > 1:
            raise InvalidError(
                f"{ref!r} matches more than one indexed commit; use more characters.",
                code="ambiguous_ref",
            )
        if matches:
            return matches[0]
    found = await db.scalar(
        browsable.where(Snapshot.ref == ref).order_by(Snapshot.ready_at.desc()).limit(1)
    )
    if found is None:
        raise _not_indexed(ref)
    return found


async def _first_run_snapshot(db: AsyncSession, repo: Repo) -> Snapshot | None:
    """The snapshot of the repository's run in progress, once its files are discovered."""
    return await db.scalar(
        select(Snapshot)
        .join(IndexJob, IndexJob.snapshot_id == Snapshot.id)
        .where(
            IndexJob.repo_id == repo.id,
            IndexJob.status.in_(ACTIVE_JOB_STATUSES),
            IndexJob.stages["discover"]["status"].astext == "done",
            Snapshot.repo_id == repo.id,
            Snapshot.status == SnapshotStatus.READY,
        )
        .limit(1)
    )


async def ref_heads(db: AsyncSession, repo: Repo) -> dict[str, Snapshot]:
    """Each ref's latest ready snapshot but the default branch's (a tracked branch's, a pull
    request's: 5.4), by ref. What a ``ref`` reads: kept searchable when the default branch's
    index moves on."""
    found = await db.scalars(
        select(Snapshot)
        .where(
            Snapshot.repo_id == repo.id,
            Snapshot.ref != repo.default_branch,
            Snapshot.status == SnapshotStatus.READY,
        )
        .order_by(Snapshot.ref, Snapshot.ready_at.desc().nulls_last(), Snapshot.created_at.desc())
        .distinct(Snapshot.ref)
    )
    return {snapshot.ref: snapshot for snapshot in found}
