"""The clone stage end to end: test database, local object storage, a local bare repository.

GitHub's REST API (branch → commit) is mocked; git talks to the bare repository over file://.
Synchronous like a worker; database helpers run on the worker runtime's event loop.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import delete, select, update

from sensei_core.config import Settings
from sensei_core.db import Database
from sensei_core.models import (
    IndexJob,
    JobKind,
    JobStatus,
    Org,
    Repo,
    RepoSource,
    Snapshot,
    SnapshotStatus,
)
from sensei_core.storage import Bucket, repo_prefix, snapshot_key
from sensei_workers.runtime import Runtime, set_runtime
from sensei_workers.tasks.ingest import clone_snapshot

pytestmark = pytest.mark.integration

COMMITS_URL = "https://api.github.com/repos/acme-gh/demo/commits/main"


@pytest.fixture
def runtime(migrated_test_db: Settings, bare_repo: Any, tmp_path: Path) -> Iterator[Runtime]:
    settings = migrated_test_db.model_copy(
        update={
            "ingest": migrated_test_db.ingest.model_copy(
                update={"cache_dir": tmp_path / "cache", "allow_file_protocol": True}
            ),
            "github": migrated_test_db.github.model_copy(
                update={"web_url": f"file://{bare_repo.web_root}"}
            ),
        }
    )
    current = Runtime(settings)
    set_runtime(current)
    yield current
    set_runtime(None)


@pytest.fixture
def github(bare_repo: Any) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(COMMITS_URL).mock(
            return_value=httpx.Response(200, json={"sha": bare_repo.second})
        )
        yield router


async def _setup(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    async with database.session() as db:
        org = Org(slug=f"clone-{uuid.uuid4().hex[:8]}", name="Clone")
        db.add(org)
        await db.flush()
        repo = Repo(
            org_id=org.id,
            source=RepoSource.PUBLIC_URL,
            github_repo_id=42,
            owner="acme-gh",
            name="demo",
            full_name="acme-gh/demo",
            slug="demo",
            default_branch="main",
            visibility="public",
        )
        db.add(repo)
        await db.flush()
        return org.id, repo.id


async def _new_job(database: Database, org_id: uuid.UUID, repo_id: uuid.UUID) -> uuid.UUID:
    async with database.session() as db:
        await db.execute(
            update(IndexJob)
            .where(IndexJob.repo_id == repo_id, IndexJob.status.in_(["queued", "running"]))
            .values(status=JobStatus.CANCELLED)
        )
        job = IndexJob(org_id=org_id, repo_id=repo_id, kind=JobKind.FULL, status=JobStatus.QUEUED)
        db.add(job)
        await db.flush()
        return job.id


async def _job(database: Database, job_id: uuid.UUID) -> IndexJob:
    async with database.session() as db:
        job = await db.get(IndexJob, job_id)
        assert job is not None
        return job


async def _snapshot(database: Database, repo_id: uuid.UUID) -> Snapshot | None:
    async with database.session() as db:
        return await db.scalar(select(Snapshot).where(Snapshot.repo_id == repo_id))


async def _cleanup(runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID) -> None:
    storage = await runtime.storage()
    await storage.delete_prefix(Bucket.SNAPSHOTS, repo_prefix(Bucket.SNAPSHOTS, org_id, repo_id))
    async with runtime.system_database.session() as db:
        await db.execute(delete(Org).where(Org.id == org_id))


@pytest.fixture
def repo(runtime: Runtime) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    ids = runtime.run(_setup(runtime.system_database))
    yield ids
    runtime.run(_cleanup(runtime, *ids))


def _clone(org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID) -> str | None:
    return clone_snapshot.apply(
        kwargs={"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    ).get()


def test_clone_archives_the_branch_head(
    runtime: Runtime,
    repo: tuple[uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id = repo
    system = runtime.system_database
    job_id = runtime.run(_new_job(system, org_id, repo_id))
    assert _clone(org_id, repo_id, job_id) == bare_repo.second

    job = runtime.run(_job(system, job_id))
    clone = job.stages["clone"]
    assert (job.status, job.stage, clone["status"]) == (JobStatus.RUNNING, "clone", "done")
    assert (clone["done"], clone["total"]) == (5, 5)
    details = clone["details"]
    assert details["commit_sha"] == bare_repo.second
    assert (details["submodules_skipped"], details["lfs"]) == (1, "pointers_only")
    assert details["archive_bytes"] > 0

    snapshot = runtime.run(_snapshot(system, repo_id))
    assert snapshot is not None
    assert (snapshot.status, snapshot.commit_sha, snapshot.ref) == (
        SnapshotStatus.READY,
        bare_repo.second,
        "main",
    )
    assert snapshot.file_count == details["files"] > 0
    assert job.snapshot_id == snapshot.id

    async def archived() -> int | None:
        storage = await runtime.storage()
        return await storage.size(Bucket.SNAPSHOTS, snapshot_key(org_id, repo_id, bare_repo.second))

    assert runtime.run(archived()) == details["archive_bytes"]
    # The working copy stays in the cache for the next run.
    assert (runtime.settings.ingest.cache_dir / str(repo_id) / "src" / "app.py").exists()


def test_an_archived_commit_is_reused(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    system = runtime.system_database
    _clone(org_id, repo_id, runtime.run(_new_job(system, org_id, repo_id)))
    second_job = runtime.run(_new_job(system, org_id, repo_id))
    _clone(org_id, repo_id, second_job)
    details = runtime.run(_job(system, second_job)).stages["clone"]["details"]
    assert "archive_bytes" not in details  # nothing archived or uploaded again


def test_an_oversized_repository_fails_the_job(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    runtime.settings = runtime.settings.model_copy(
        update={"ingest": runtime.settings.ingest.model_copy(update={"max_checkout_bytes": 10})}
    )
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    assert _clone(org_id, repo_id, job_id) is None
    job = runtime.run(_job(runtime.system_database, job_id))
    assert job.status is JobStatus.FAILED
    assert job.error is not None
    assert (job.error["stage"], job.error["code"]) == ("clone", "repo_too_large")
    assert "limit" in job.error["message"]


def test_an_unknown_branch_fails_the_job(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    github.get(COMMITS_URL).mock(return_value=httpx.Response(404, json={"message": "No commit"}))
    org_id, repo_id = repo
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    assert _clone(org_id, repo_id, job_id) is None
    job = runtime.run(_job(runtime.system_database, job_id))
    assert (job.status, (job.error or {}).get("code")) == (JobStatus.FAILED, "ref_not_found")


def test_a_finished_or_cancelled_job_is_left_alone(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    runtime.run(_new_job(runtime.system_database, org_id, repo_id))  # cancels the first
    assert _clone(org_id, repo_id, job_id) is None
    assert not github.routes[0].called
