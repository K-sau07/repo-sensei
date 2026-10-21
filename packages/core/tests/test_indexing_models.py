"""Snapshots and index jobs in Postgres: constraints, JSON progress, row-level security."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    IndexJob,
    JobKind,
    JobStatus,
    Repo,
    RepoSource,
    Snapshot,
    Stage,
)
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user

pytestmark = pytest.mark.integration


async def _repo(db: AsyncSession) -> Repo:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=1,
        owner="o",
        name="r",
        full_name="o/r",
        slug="r",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()
    return repo


async def _insert(db: AsyncSession, row: object) -> None:
    async with db.begin_nested():
        db.add(row)
        await db.flush()


def _job(repo: Repo, status: JobStatus = JobStatus.QUEUED) -> IndexJob:
    return IndexJob(org_id=repo.org_id, repo_id=repo.id, kind=JobKind.FULL, status=status)


async def test_one_snapshot_per_commit(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    sha = "c" * 40
    db_session.add(Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="main", commit_sha=sha))
    await db_session.flush()
    duplicate = Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="dev", commit_sha=sha)
    with pytest.raises(IntegrityError):
        await _insert(db_session, duplicate)


async def test_one_active_job_per_repository(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    db_session.add(_job(repo))
    await db_session.flush()
    for status in (JobStatus.QUEUED, JobStatus.RUNNING):
        with pytest.raises(IntegrityError):
            await _insert(db_session, _job(repo, status))
    # Finished jobs don't count: history accumulates.
    for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
        db_session.add(_job(repo, status))
    await db_session.flush()


async def test_progress_is_persisted(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    job = _job(repo)
    db_session.add(job)
    await db_session.flush()
    machine = IndexJobStateMachine(job)
    machine.start()
    machine.advance(Stage.DISCOVER)
    machine.update_progress(Stage.DISCOVER, 40, 80)
    expected_progress = job.progress
    await db_session.flush()
    await db_session.refresh(job)  # read back what Postgres stored
    assert (job.status, job.stage) == (JobStatus.RUNNING, Stage.DISCOVER)
    assert job.stages["clone"]["status"] == "done"
    assert (job.stages["discover"]["done"], job.stages["discover"]["total"]) == (40, 80)
    assert job.progress == expected_progress > 0


async def test_jobs_and_snapshots_are_org_scoped(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    db_session.add(_job(repo))
    db_session.add(Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="main", commit_sha="d" * 40))
    await db_session.flush()
    await set_org_context(db_session, uuid.uuid4())  # another org
    assert (await db_session.scalars(select(IndexJob))).all() == []
    assert (await db_session.scalars(select(Snapshot))).all() == []


async def test_a_snapshot_cannot_point_at_another_orgs_repository(
    owner_session: AsyncSession,
) -> None:
    repo = await _repo(owner_session)
    other = await _repo(owner_session)
    # Foreign keys ignore row-level security; the (repo, org) pair is what stops this.
    stray = Snapshot(org_id=other.org_id, repo_id=repo.id, ref="main", commit_sha="e" * 40)
    with pytest.raises(IntegrityError, match="fk_snapshots_repo_id_repos"):
        await _insert(owner_session, stray)
