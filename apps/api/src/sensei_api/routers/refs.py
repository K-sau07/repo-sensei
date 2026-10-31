"""A repository's refs (5.4): its default branch, the branches it tracks and the pull requests it
indexes (an opt-in setting), each with what its index is: the commit indexed and when, whether a
run is going or the last one failed, and for an overlay the default branch's commit it was built
on. What the ref switcher lists; any of them can be passed as ``ref`` to the read endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from sensei_api.deps import DbSession
from sensei_api.repos import RepoDep
from sensei_core.indexing.pushes import PULLS
from sensei_core.indexing.snapshots import ref_heads
from sensei_core.models import ACTIVE_JOB_STATUSES, IndexJob, JobStatus, Snapshot
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.repos.service import RepoSettings

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["refs"])

RefStatus = Literal["indexed", "indexing", "failed", "not_indexed"]
RECENT_JOBS = 200
ACTIVE = frozenset(ACTIVE_JOB_STATUSES)


class RefOut(BaseModel):
    name: str  # main, release, pull/12
    kind: Literal["branch", "pull_request"]
    default: bool
    status: RefStatus
    sha: str | None  # the commit its index is of (None: not indexed)
    indexed_at: datetime | None
    overlay_of: str | None  # an overlay's: the default branch's commit it was built on


class RefList(BaseModel):
    refs: list[RefOut]


@router.get("/refs", response_model=RefList)
async def refs(*, repo: RepoDep, db: DbSession) -> RefList:
    """The default branch first, then the tracked branches and the pull requests, by name."""
    settings = RepoSettings.model_validate(repo.settings or {})
    heads = await ref_heads(db, repo)
    indexed = (
        await db.scalar(
            select(Snapshot).where(
                Snapshot.repo_id == repo.id,
                Snapshot.commit_sha == repo.last_indexed_sha,
                Snapshot.status == SnapshotStatus.READY,
            )
        )
        if repo.last_indexed_sha
        else None
    )
    jobs = list(
        await db.scalars(
            select(IndexJob)
            .where(IndexJob.repo_id == repo.id)
            .order_by(IndexJob.created_at.desc())
            .limit(RECENT_JOBS)
        )
    )
    latest: dict[str, IndexJob] = {}
    for job in jobs:  # newest first: each ref's latest run
        latest.setdefault(job.ref or repo.default_branch, job)
    bases = {
        s.id: s.commit_sha
        for s in await db.scalars(
            select(Snapshot).where(
                Snapshot.id.in_([h.overlay_of for h in heads.values() if h.overlay_of])
            )
        )
    }
    names = [
        repo.default_branch,
        *sorted(set(settings.tracked_branches) - {repo.default_branch}),
        *sorted(
            # The pull requests it indexes: an overlay kept, or one being built (a closed one's
            # is dropped, and its runs don't list it again).
            {
                name
                for name in [*heads, *(n for n, j in latest.items() if j.status in ACTIVE)]
                if name.startswith(PULLS)
            }
            if settings.index_pull_requests
            else set(),
            key=lambda name: int(name.removeprefix(PULLS)) if name[len(PULLS) :].isdigit() else 0,
        ),
    ]
    out: list[RefOut] = []
    for name in names:
        default = name == repo.default_branch
        head = indexed if default else heads.get(name)
        job = latest.get(name)
        status: RefStatus = "indexed" if head is not None else "not_indexed"
        if job is not None and job.status in ACTIVE_JOB_STATUSES:
            status = "indexing"
        elif job is not None and job.status is JobStatus.FAILED:
            status = "failed"
        out.append(
            RefOut(
                name=name,
                kind="pull_request" if name.startswith(PULLS) else "branch",
                default=default,
                status=status,
                sha=head.commit_sha if head is not None else None,
                indexed_at=head.ready_at if head is not None else None,
                overlay_of=bases.get(head.overlay_of) if head and head.overlay_of else None,
            )
        )
    return RefList(refs=out)
