"""The clone and discover stages end to end: test database, local object storage, a local bare
repository.

GitHub's REST API (branch → commit) is mocked; git talks to the bare repository over file://.
Synchronous like a worker; database helpers run on the worker runtime's event loop.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import delete, select, update

from sensei_core.config import Settings
from sensei_core.db import Database
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.models import (
    IndexJob,
    JobKind,
    JobStatus,
    Org,
    Repo,
    RepoFile,
    RepoSource,
    RepoStatus,
    Snapshot,
    SnapshotStatus,
    Stage,
)
from sensei_core.storage import Bucket, artifact_key, repo_prefix, snapshot_key
from sensei_engine.ingest.merkle import ARTIFACT_NAME, MAX_STORED_BYTES, MerkleTree
from sensei_workers.runtime import Runtime, set_runtime
from sensei_workers.tasks.discover import discover_files
from sensei_workers.tasks.ingest import clone_snapshot
from sensei_workers.tasks.merkle import build_merkle
from sensei_workers.tasks.orchestrate import sweep_stalled_jobs

pytestmark = pytest.mark.integration

COMMITS_URL = "https://api.github.com/repos/acme-gh/demo/commits/main"
REPO_URL = "https://api.github.com/repos/acme-gh/demo"


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
    for bucket in Bucket:
        await storage.delete_prefix(bucket, repo_prefix(bucket, org_id, repo_id))
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
    github.get(REPO_URL).mock(return_value=httpx.Response(200, json={"id": 42}))
    org_id, repo_id = repo
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    assert _clone(org_id, repo_id, job_id) is None
    job = runtime.run(_job(runtime.system_database, job_id))
    assert (job.status, (job.error or {}).get("code")) == (JobStatus.FAILED, "ref_not_found")


def test_a_public_repository_gone_private_loses_access(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    github.get(COMMITS_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    github.get(REPO_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    org_id, repo_id = repo
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    assert _clone(org_id, repo_id, job_id) is None
    job = runtime.run(_job(runtime.system_database, job_id))
    assert (job.error or {}).get("code") == "repo_not_public"
    # Members no longer read it just for being members (see RepoAccess).
    assert runtime.run(_repo(runtime.system_database, repo_id)).status is RepoStatus.ACCESS_LOST


def test_a_repository_far_too_large_on_github_is_never_fetched(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    runtime.run(_set_size(runtime.system_database, repo_id, 30 * 1024**2))  # 30 GB
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    assert _clone(org_id, repo_id, job_id) is None
    job = runtime.run(_job(runtime.system_database, job_id))
    assert (job.error or {}).get("code") == "repo_too_large"
    assert not github.routes[0].called  # not even the branch was looked up


async def _set_size(database: Database, repo_id: uuid.UUID, size_kb: int) -> None:
    async with database.session() as db:
        await db.execute(update(Repo).where(Repo.id == repo_id).values(size_kb=size_kb))


def test_a_finished_or_cancelled_job_is_left_alone(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    runtime.run(_new_job(runtime.system_database, org_id, repo_id))  # cancels the first
    assert _clone(org_id, repo_id, job_id) is None
    assert not github.routes[0].called


# ------------------------------------------------------------------ discover


def _discover(org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID) -> int | None:
    return discover_files.apply(
        kwargs={"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    ).get()


async def _files(database: Database, repo_id: uuid.UUID) -> dict[str, RepoFile]:
    async with database.session() as db:
        rows = await db.scalars(select(RepoFile).where(RepoFile.repo_id == repo_id))
        return {row.path: row for row in rows}


async def _repo(database: Database, repo_id: uuid.UUID) -> Repo:
    async with database.session() as db:
        repo = await db.get(Repo, repo_id)
        assert repo is not None
        return repo


async def _set_ignore(database: Database, repo_id: uuid.UUID, globs: list[str]) -> None:
    async with database.session() as db:
        await db.execute(
            update(Repo).where(Repo.id == repo_id).values(settings={"ignore_globs": globs})
        )


def _cloned(runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID) -> uuid.UUID:
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    assert _clone(org_id, repo_id, job_id) is not None
    return job_id


SECOND_COMMIT_FILES = [".gitattributes", "README.md", "model.bin", "run.sh", "src/app.py"]


def test_discover_records_the_snapshot_files(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    system = runtime.system_database
    job_id = _cloned(runtime, org_id, repo_id)
    assert _discover(org_id, repo_id, job_id) == len(SECOND_COMMIT_FILES)

    job = runtime.run(_job(system, job_id))
    discover = job.stages["discover"]
    assert (job.status, job.stage, discover["status"]) == (JobStatus.RUNNING, "discover", "done")
    assert discover["details"]["files"] == len(SECOND_COMMIT_FILES)
    assert discover["details"]["skipped"]["symlinks"] == 1  # "outside" points out of the tree
    assert set(discover["details"]["languages"]) == {"Python", "Shell"}
    assert discover["details"]["services"] == []
    files = runtime.run(_files(system, repo_id))
    assert sorted(files) == SECOND_COMMIT_FILES
    app = files["src/app.py"]
    assert (app.language, app.role, app.loc, app.snapshot_id) == (
        "python",
        "service",
        1,
        job.snapshot_id,
    )
    assert files["model.bin"].flags == ["lfs_pointer"]
    assert files["run.sh"].role == "config"

    snapshot = runtime.run(_snapshot(system, repo_id))
    assert snapshot is not None
    assert snapshot.file_count == len(SECOND_COMMIT_FILES)
    assert snapshot.summary is not None
    assert snapshot.summary["roles"]["service"]["files"] == 1
    languages = runtime.run(_repo(system, repo_id)).languages
    assert languages == {"Python": files["src/app.py"].size, "Shell": files["run.sh"].size}

    # A duplicate delivery after the stage is done changes nothing.
    assert _discover(org_id, repo_id, job_id) is None


def test_discover_unpacks_the_archive_without_the_working_copy(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    job_id = _cloned(runtime, org_id, repo_id)
    # Another worker took the clone: this one's cache doesn't have the copy.
    shutil.rmtree(runtime.settings.ingest.cache_dir / str(repo_id))
    assert _discover(org_id, repo_id, job_id) == len(SECOND_COMMIT_FILES)
    assert sorted(runtime.run(_files(runtime.system_database, repo_id))) == SECOND_COMMIT_FILES
    assert (runtime.settings.ingest.cache_dir / str(repo_id) / "src" / "app.py").exists()


def test_discover_follows_the_repository_ignore_globs(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    runtime.run(_set_ignore(runtime.system_database, repo_id, ["*.md", "src/"]))
    job_id = _cloned(runtime, org_id, repo_id)
    assert _discover(org_id, repo_id, job_id) == 3
    assert sorted(runtime.run(_files(runtime.system_database, repo_id))) == [
        ".gitattributes",
        "model.bin",
        "run.sh",
    ]


def test_too_many_files_fails_the_job(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    job_id = _cloned(runtime, org_id, repo_id)
    runtime.settings = runtime.settings.model_copy(
        update={"ingest": runtime.settings.ingest.model_copy(update={"max_files": 2})}
    )
    assert _discover(org_id, repo_id, job_id) is None
    job = runtime.run(_job(runtime.system_database, job_id))
    assert job.status is JobStatus.FAILED
    assert job.error is not None
    assert (job.error["stage"], job.error["code"]) == ("discover", "too_many_files")
    assert runtime.run(_files(runtime.system_database, repo_id)) == {}


def test_discover_waits_for_the_clone(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    job_id = runtime.run(_new_job(runtime.system_database, org_id, repo_id))
    assert _discover(org_id, repo_id, job_id) is None
    job = runtime.run(_job(runtime.system_database, job_id))
    assert (job.status, job.stage) == (JobStatus.QUEUED, None)


# ------------------------------------------------------------------ merkle


def _merkle(org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID) -> str | None:
    return build_merkle.apply(
        kwargs={"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    ).get()


def _stored_tree(runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID, sha: str) -> MerkleTree:
    async def load() -> bytes | None:
        storage = await runtime.storage()
        key = artifact_key(org_id, repo_id, sha, ARTIFACT_NAME)
        return await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=MAX_STORED_BYTES)

    data = runtime.run(load())
    assert data is not None
    return MerkleTree.from_bytes(data)


async def _indexed_at(database: Database, repo_id: uuid.UUID, sha: str) -> None:
    async with database.session() as db:
        await db.execute(update(Repo).where(Repo.id == repo_id).values(last_indexed_sha=sha))


def test_merkle_stores_the_tree_and_records_the_root(
    runtime: Runtime,
    repo: tuple[uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id = repo
    system = runtime.system_database
    job_id = _cloned(runtime, org_id, repo_id)
    _discover(org_id, repo_id, job_id)
    root = _merkle(org_id, repo_id, job_id)
    assert root is not None

    job = runtime.run(_job(system, job_id))
    merkle = job.stages["merkle"]
    assert (job.stage, merkle["status"], merkle["done"]) == ("merkle", "done", 3)
    assert (merkle["details"]["root"], merkle["details"]["files"]) == (root, 5)
    assert "since" not in merkle["details"]  # never indexed before
    snapshot = runtime.run(_snapshot(system, repo_id))
    assert snapshot is not None
    assert snapshot.merkle_root == root

    tree = _stored_tree(runtime, org_id, repo_id, bare_repo.second)
    assert tree.root.hex() == root
    files = runtime.run(_files(system, repo_id))
    assert sorted(tree.files) == sorted(files)
    leaf = tree.files["src/app.py"]
    assert (leaf.content_hash, leaf.fast_hash) == (
        files["src/app.py"].content_hash,
        files["src/app.py"].fast_hash,
    )
    assert _merkle(org_id, repo_id, job_id) is None  # a duplicate delivery


def test_merkle_notes_what_changed_since_the_last_index(
    runtime: Runtime,
    repo: tuple[uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id = repo
    system = runtime.system_database
    github.get(COMMITS_URL).mock(return_value=httpx.Response(200, json={"sha": bare_repo.first}))
    first_job = _cloned(runtime, org_id, repo_id)
    _discover(org_id, repo_id, first_job)
    _merkle(org_id, repo_id, first_job)
    runtime.run(_indexed_at(system, repo_id, bare_repo.first))  # what finalize (1.9) will do

    github.get(COMMITS_URL).mock(return_value=httpx.Response(200, json={"sha": bare_repo.second}))
    second_job = _cloned(runtime, org_id, repo_id)
    _discover(org_id, repo_id, second_job)
    _merkle(org_id, repo_id, second_job)
    since = runtime.run(_job(system, second_job)).stages["merkle"]["details"]["since"]
    # first -> second: old.txt deleted, src/app.py changed, .gitattributes and model.bin added.
    assert since == {
        "commit_sha": bare_repo.first,
        "added": 2,
        "modified": 1,
        "deleted": 1,
        "renamed": 0,
    }


def test_merkle_waits_for_discover(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID], github: respx.MockRouter
) -> None:
    org_id, repo_id = repo
    job_id = _cloned(runtime, org_id, repo_id)
    assert _merkle(org_id, repo_id, job_id) is None
    job = runtime.run(_job(runtime.system_database, job_id))
    assert (job.stage, job.stages["clone"]["status"]) == ("clone", "done")


# ------------------------------------------------------------------ giving up


async def _running_at_discover(
    database: Database, org_id: uuid.UUID, repo_id: uuid.UUID, started: datetime
) -> uuid.UUID:
    job_id = await _new_job(database, org_id, repo_id)
    async with database.session() as db:
        job = await db.get(IndexJob, job_id)
        assert job is not None
        machine = IndexJobStateMachine(job, now=lambda: started)
        machine.start()
        machine.advance(Stage.DISCOVER)
    return job_id


def test_a_dead_lettered_task_fails_its_job(
    runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID]
) -> None:
    org_id, repo_id = repo
    job_id = runtime.run(_running_at_discover(runtime.system_database, org_id, repo_id, _utc()))
    kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    discover_files.on_dead_letter("task-1", (), kwargs)  # the worker kept dying
    job = runtime.run(_job(runtime.system_database, job_id))
    assert job.status is JobStatus.FAILED
    assert (job.error or {}).get("code") == "worker_lost"


def test_stalled_jobs_are_failed(runtime: Runtime, repo: tuple[uuid.UUID, uuid.UUID]) -> None:
    org_id, repo_id = repo
    system = runtime.system_database
    stalled = runtime.run(
        _running_at_discover(system, org_id, repo_id, _utc() - timedelta(hours=2))
    )
    assert sweep_stalled_jobs.apply().get() >= 1
    job = runtime.run(_job(system, stalled))
    assert (job.status, (job.error or {}).get("code")) == (JobStatus.FAILED, "stage_stalled")
    # One that moved recently is left alone.
    fresh = runtime.run(_running_at_discover(system, org_id, repo_id, _utc()))
    sweep_stalled_jobs.apply().get()
    assert runtime.run(_job(system, fresh)).status is JobStatus.RUNNING


def _utc() -> datetime:
    return datetime.now(UTC)
