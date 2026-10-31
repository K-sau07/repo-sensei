"""Which indexed snapshot a ``ref`` means: the last index, a commit (or its prefix), a branch."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    IndexJob,
    JobStatus,
    Repo,
    RepoRef,
    Snapshot,
    SnapshotStatus,
)

SHA_PREFIX = re.compile(r"[0-9a-f]{7,64}")
MAX_REF_LENGTH = 255


def _not_indexed(ref: str | None) -> NotFoundError:
    if ref is None:
        return NotFoundError("This repository hasn't been indexed yet.", code="repo_not_indexed")
    return NotFoundError(f"Nothing indexed at {ref!r}.", code="ref_not_indexed")


async def resolve_snapshot(db: AsyncSession, repo: Repo, ref: str | None) -> Snapshot:
    """The browsable snapshot for ``ref``: None means the last index; a hex string of 7 or more
    characters is a commit id (or the start of one); anything else is a branch or a pull
    request, read at what its ``repo_refs`` row points at. Only snapshots an index run finished
    count (one left by a failed or cancelled run may have its tree, but was never indexed)."""
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
    if SHA_PREFIX.fullmatch(ref):
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
    # The ref's index, as its runs left it (5.4): a ref is read only while it has one (one the
    # repository stopped indexing, or a closed pull request, has none); the default branch's is
    # the last index.
    pointed = select(RepoRef.snapshot_id).where(RepoRef.repo_id == repo.id, RepoRef.ref == ref)
    found = await db.scalar(browsable.where(Snapshot.id.in_(pointed)))
    if found is None and ref == repo.default_branch and repo.last_indexed_sha is not None:
        found = await db.scalar(browsable.where(Snapshot.commit_sha == repo.last_indexed_sha))
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
    """What each ref but the default branch reads (a tracked branch, a pull request: 5.4), by
    ref: kept searchable, and kept at all, when the default branch's index moves on."""
    rows = await db.execute(
        select(RepoRef.ref, Snapshot)
        .join(Snapshot, Snapshot.id == RepoRef.snapshot_id)
        .where(
            RepoRef.repo_id == repo.id,
            RepoRef.ref != repo.default_branch,
            Snapshot.status == SnapshotStatus.READY,
        )
        .order_by(RepoRef.ref)
    )
    found: dict[str, Snapshot] = dict(rows.tuples().all())
    return found


async def ref_snapshot(db: AsyncSession, repo: Repo, ref: str) -> Snapshot | None:
    """The snapshot ``ref`` reads now (its ``repo_refs`` row), if any."""
    return await db.scalar(
        select(Snapshot)
        .join(RepoRef, RepoRef.snapshot_id == Snapshot.id)
        .where(RepoRef.repo_id == repo.id, RepoRef.ref == ref)
    )


async def point_ref(db: AsyncSession, repo: Repo, ref: str, snapshot: Snapshot) -> None:
    """``ref`` reads ``snapshot`` from now on (its run finished, or its commit was indexed)."""
    await db.execute(
        pg_insert(RepoRef)
        .values(org_id=repo.org_id, repo_id=repo.id, ref=ref, snapshot_id=snapshot.id)
        .on_conflict_do_update(
            index_elements=[RepoRef.repo_id, RepoRef.ref],
            set_={"snapshot_id": snapshot.id, "updated_at": func.now()},
        )
    )
