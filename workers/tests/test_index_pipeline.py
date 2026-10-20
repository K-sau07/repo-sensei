"""A whole index run through the chain (eager): stages in order, events on the job's stream,
the repository ready at the commit; and cancelling while the clone runs."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import orjson
import pytest
import respx
from sqlalchemy import delete, select

from sensei_core.config import Settings
from sensei_core.db import Database
from sensei_core.indexing.events import JobOutbox, job_stream, org_stream
from sensei_core.indexing.jobs import settle_repo
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.models import IndexJob, JobKind, JobStatus, Org, Repo, RepoSource, RepoStatus
from sensei_core.storage import Bucket, repo_prefix
from sensei_workers.ingest.git import GitRunner
from sensei_workers.runtime import Runtime, set_runtime
from sensei_workers.tasks.orchestrate import run_index_job

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


async def _setup(database: Database) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with database.session() as db:
        org = Org(slug=f"pipe-{uuid.uuid4().hex[:8]}", name="Pipeline")
        db.add(org)
        await db.flush()
        repo = Repo(
            org_id=org.id,
            source=RepoSource.PUBLIC_URL,
            github_repo_id=7,
            owner="acme-gh",
            name="demo",
            full_name="acme-gh/demo",
            slug="demo",
            default_branch="main",
            visibility="public",
        )
        db.add(repo)
        await db.flush()
        job = IndexJob(org_id=org.id, repo_id=repo.id, kind=JobKind.FULL)
        db.add(job)
        await db.flush()
        return org.id, repo.id, job.id


async def _cleanup(runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID) -> None:
    storage = await runtime.storage()
    for bucket in Bucket:
        await storage.delete_prefix(bucket, repo_prefix(bucket, org_id, repo_id))
    async with runtime.system_database.session() as db:
        await db.execute(delete(Org).where(Org.id == org_id))


@pytest.fixture
def ids(runtime: Runtime) -> Iterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    found = runtime.run(_setup(runtime.system_database))
    yield found
    runtime.run(_cleanup(runtime, found[0], found[1]))


async def _events(runtime: Runtime, key: str) -> list[dict[str, Any]]:
    entries = cast("list[tuple[str, dict[str, str]]]", await runtime.redis.xrange(key))
    return [orjson.loads(fields["data"]) for _, fields in entries]


async def _state(
    database: Database, repo_id: uuid.UUID, job_id: uuid.UUID
) -> tuple[Repo, IndexJob]:
    async with database.session() as db:
        repo = await db.get(Repo, repo_id)
        job = await db.get(IndexJob, job_id)
        assert repo is not None
        assert job is not None
        return repo, job


def _run(org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID) -> None:
    kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    run_index_job.apply(kwargs=kwargs).get()


def test_a_full_run_streams_its_stages_in_order(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, job.stage, job.progress) == (JobStatus.SUCCEEDED, "finalize", 1.0)
    assert (repo.status, repo.last_indexed_sha) == (RepoStatus.READY, bare_repo.second)
    # The stages this phase doesn't have yet are recorded as skipped.
    assert job.stages["parse"]["status"] == job.stages["links"]["status"] == "skipped"

    events = runtime.run(_events(runtime, job_stream(job_id)))
    milestones = [(e["type"], e["stage"]) for e in events if e["type"] not in ("progress", "log")]
    assert milestones == [
        ("stage_started", "clone"),
        ("stage_done", "clone"),
        ("stage_started", "discover"),
        ("stage_done", "discover"),
        ("unlock", "discover"),
        ("stage_started", "merkle"),
        ("stage_done", "merkle"),
        ("stage_started", "finalize"),
        ("stage_done", "finalize"),
        ("done", "finalize"),
    ]
    unlock = next(e for e in events if e["type"] == "unlock")
    assert unlock["unlocks"] == ["code_browser"]
    progress = [e for e in events if e["type"] == "progress"]
    assert progress
    assert all(e["done"] <= e["total"] for e in progress)
    logs = [e["message"] for e in events if e["type"] == "log"]
    assert any(m.startswith("Checked out") for m in logs)
    assert any(m.startswith("Found 5 files") for m in logs)
    assert logs[-1].startswith("Indexed")
    assert {e["job_id"] for e in events} == {str(job_id)}

    # The org's stream has the lifecycle only: each stage start and the end.
    org = [(e["type"], e["stage"]) for e in runtime.run(_events(runtime, org_stream(org_id)))]
    assert org[-1] == ("done", "finalize")
    assert all(kind in ("stage_started", "done") for kind, _ in org)


def test_cancelling_while_cloning_stops_the_rest(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, repo_id, job_id = ids
    real_checkout = GitRunner.checkout

    async def cancel() -> None:
        outbox = JobOutbox()
        async with runtime.system_database.session() as db:
            job = await db.scalar(select(IndexJob).where(IndexJob.id == job_id).with_for_update())
            repo = await db.get(Repo, repo_id)
            assert job is not None
            assert repo is not None
            machine = IndexJobStateMachine(job)
            machine.cancel()  # what POST …/cancel does, while git is still fetching
            settle_repo(repo, JobStatus.CANCELLED)
            outbox.collect(machine)
        await outbox.publish(runtime.redis)

    def checkout_then_cancel(self: GitRunner, *args: Any, **kwargs: Any) -> Any:
        result = real_checkout(self, *args, **kwargs)
        runtime.run(cancel())
        return result

    monkeypatch.setattr(GitRunner, "checkout", checkout_then_cancel)
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.CANCELLED
    assert job.stages["clone"]["status"] == "cancelled"
    assert "discover" not in job.stages  # nothing after the clone ran
    assert repo.status is RepoStatus.PENDING  # never indexed before
    events = runtime.run(_events(runtime, job_stream(job_id)))
    assert events[-1]["type"] == "cancelled"
    assert not [e for e in events if e["stage"] == "discover"]
