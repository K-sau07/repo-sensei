"""Index jobs as the API sees them: start one, cancel one, read their history.

A repository has at most one queued or running job (a partial unique index), so a second start
answers 409. Starting only creates the row; the caller sends the work to the workers once the
transaction has committed (:meth:`sensei_core.jobs.RepoJobs.index`), and events are published
then too. What the repository's status becomes when a job ends lives here as well
(:func:`settle_repo`), for the workers and the API alike.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.errors import ConflictError, NotFoundError
from sensei_core.indexing.events import JobOutbox
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    IndexJob,
    JobKind,
    JobStatus,
    Repo,
    RepoStatus,
    Role,
    Snapshot,
)
from sensei_core.orgs.service import OrgAccess, require_role

# What an index job's paid usage (embeddings, summaries) is recorded against, with the job id:
# its stages write it, finalize sums it.
JOB_USAGE_REF = "index_job"

HISTORY_LIMIT = 50


@dataclass(frozen=True, slots=True)
class RepoSummary:
    snapshot: Snapshot | None  # the last index
    job: IndexJob | None  # queued or running


async def repo_summaries(db: AsyncSession, repos: list[Repo]) -> dict[uuid.UUID, RepoSummary]:
    """The last indexed snapshot and the active job of each repository, in two queries."""
    if not repos:
        return {}
    shas = {repo.id: repo.last_indexed_sha for repo in repos if repo.last_indexed_sha}
    snapshots: dict[uuid.UUID, Snapshot] = {}
    if shas:
        rows = await db.scalars(
            select(Snapshot).where(
                Snapshot.repo_id.in_(list(shas)), Snapshot.commit_sha.in_(set(shas.values()))
            )
        )
        snapshots = {s.repo_id: s for s in rows if shas.get(s.repo_id) == s.commit_sha}
    jobs = {
        job.repo_id: job
        for job in await db.scalars(
            select(IndexJob).where(
                IndexJob.repo_id.in_([repo.id for repo in repos]),
                IndexJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
    }
    return {
        repo.id: RepoSummary(snapshot=snapshots.get(repo.id), job=jobs.get(repo.id))
        for repo in repos
    }


def settle_repo(repo: Repo, outcome: JobStatus) -> None:
    """The repository's status once a job ends. A failed or cancelled run leaves an earlier
    index in place (still ``ready``); lost access always wins."""
    if repo.status is RepoStatus.ACCESS_LOST or repo.deleted_at is not None:
        return
    if outcome is JobStatus.SUCCEEDED or repo.last_indexed_sha is not None:
        repo.status = RepoStatus.READY
    else:
        repo.status = RepoStatus.ERROR if outcome is JobStatus.FAILED else RepoStatus.PENDING


class IndexJobService:
    async def start(
        self,
        db: AsyncSession,
        access: OrgAccess,
        repo: Repo,
        outbox: JobOutbox,
        *,
        kind: JobKind = JobKind.FULL,
    ) -> IndexJob:
        """Queue an index run (admins). 409 ``index_job_running`` if one is queued or running,
        409 ``repo_access_lost`` if GitHub no longer lets us read it."""
        require_role(access, Role.ADMIN)
        if repo.status is RepoStatus.ACCESS_LOST:
            raise ConflictError(
                "repo-sensei can't read this repository on GitHub any more.",
                code="repo_access_lost",
            )
        job = IndexJob(org_id=repo.org_id, repo_id=repo.id, kind=kind, status=JobStatus.QUEUED)
        try:
            async with db.begin_nested():
                db.add(job)
                await db.flush()
        except IntegrityError as exc:  # the one-active-job index
            raise ConflictError(
                "This repository is already being indexed.", code="index_job_running"
            ) from exc
        await db.refresh(job)
        await audit(
            db,
            AuditAction.REPO_INDEX_STARTED,
            actor_user_id=access.user.id,
            org_id=repo.org_id,
            target_type="repo",
            target_id=repo.id,
            meta={"job_id": str(job.id), "kind": kind.value},
        )
        outbox.queued(job)
        return job

    async def cancel(
        self,
        db: AsyncSession,
        access: OrgAccess,
        repo: Repo,
        job_id: uuid.UUID,
        outbox: JobOutbox,
    ) -> IndexJob:
        """Cancel a queued or running job (admins); the workers stop at their next step."""
        require_role(access, Role.ADMIN)
        job = await db.scalar(
            select(IndexJob)
            .where(IndexJob.id == job_id, IndexJob.repo_id == repo.id)
            .with_for_update()
        )
        if job is None:
            raise NotFoundError("Index job not found.", code="index_job_not_found")
        if job.status not in ACTIVE_JOB_STATUSES:
            raise ConflictError("This index job has already ended.", code="index_job_finished")
        machine = IndexJobStateMachine(job)
        machine.cancel()
        settle_repo(repo, JobStatus.CANCELLED)
        outbox.collect(machine)
        await db.flush()
        await audit(
            db,
            AuditAction.REPO_INDEX_CANCELLED,
            actor_user_id=access.user.id,
            org_id=repo.org_id,
            target_type="repo",
            target_id=repo.id,
            meta={"job_id": str(job.id)},
        )
        await db.refresh(job)
        return job

    async def history(
        self, db: AsyncSession, repo: Repo, *, limit: int = HISTORY_LIMIT
    ) -> list[IndexJob]:
        rows = await db.scalars(
            select(IndexJob)
            .where(IndexJob.repo_id == repo.id)
            .order_by(IndexJob.created_at.desc(), IndexJob.id.desc())
            .limit(limit)
        )
        return list(rows)

    async def get(self, db: AsyncSession, repo: Repo, job_id: uuid.UUID) -> IndexJob:
        job = await db.scalar(
            select(IndexJob).where(IndexJob.id == job_id, IndexJob.repo_id == repo.id)
        )
        if job is None:
            raise NotFoundError("Index job not found.", code="index_job_not_found")
        return job

    async def active(self, db: AsyncSession, repo: Repo) -> IndexJob | None:
        return await db.scalar(
            select(IndexJob).where(
                IndexJob.repo_id == repo.id, IndexJob.status.in_(ACTIVE_JOB_STATUSES)
            )
        )
