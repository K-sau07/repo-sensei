"""A whole index run through the chain (eager): stages in order, events on the job's stream,
the repository ready at the commit; file summaries through a fake Claude (within the cap, over
it, and resumed after an outage); and cancelling while the clone runs."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import orjson
import pytest
import respx
from celery import chain
from celery.signals import task_postrun
from sqlalchemy import delete, func, select, text

from sensei_core.config import Settings
from sensei_core.db import Database
from sensei_core.indexing.events import JobOutbox, job_stream, org_stream
from sensei_core.indexing.jobs import settle_repo
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.jobs import TaskName
from sensei_core.models import (
    CodeChunk,
    CodeNode,
    GlossaryTerm,
    IndexJob,
    JobKind,
    JobStatus,
    Org,
    Repo,
    RepoSource,
    RepoStatus,
    Snapshot,
    Stage,
    UsageEvent,
)
from sensei_core.storage import Bucket, artifact_key, repo_prefix
from sensei_engine.chunk.store import ChunkWriter, FileChunks
from sensei_engine.ingest.store import file_ids
from sensei_engine.llm import rollups, summaries
from sensei_engine.search.bm25 import keyword_search
from sensei_testing.anthropic import FakeAnthropic, batch, jsonl, message
from sensei_workers.base import RetryableError
from sensei_workers.ingest.git import GitRunner
from sensei_workers.runtime import Runtime, set_runtime
from sensei_workers.statistics import vacuum_chunks
from sensei_workers.tasks import chunk as chunk_task
from sensei_workers.tasks import orchestrate
from sensei_workers.tasks.merkle import build_merkle
from sensei_workers.tasks.orchestrate import run_index_job
from sensei_workers.tasks.summaries import summarize_snapshot

pytestmark = pytest.mark.integration

COMMITS_URL = "https://api.github.com/repos/acme-gh/demo/commits/main"


@pytest.fixture
def runtime(
    migrated_test_db: Settings, bare_repo: Any, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Runtime]:
    """The workers' runtime; with Claude (the ``claude`` fixture, and ``cap``) when the test
    asks for them, else without (summaries are passed over)."""
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
    if "cap" in request.fixturenames:
        pricing = settings.pricing.model_copy(
            update={"org_monthly_cap_usd": request.getfixturevalue("cap")}
        )
        settings = settings.model_copy(update={"pricing": pricing})
    claude: FakeAnthropic | None = (
        request.getfixturevalue("claude") if "claude" in request.fixturenames else None
    )
    current = Runtime(settings, anthropic=claude.client() if claude is not None else None)
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


async def _nodes(database: Database, snapshot_id: uuid.UUID) -> tuple[Snapshot, list[CodeNode]]:
    async with database.session() as db:
        snapshot = await db.get(Snapshot, snapshot_id)
        assert snapshot is not None
        nodes = await db.scalars(
            select(CodeNode)
            .where(CodeNode.snapshot_id == snapshot_id)
            .order_by(CodeNode.start_line, CodeNode.end_line.desc(), CodeNode.symbol_key)
        )
        return snapshot, list(nodes)


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
    assert {job.stages[s]["status"] for s in ("parse", "symbols", "chunks")} == {"done"}
    # The stages this phase doesn't have yet are recorded as skipped; so are summaries, with no
    # Anthropic key.
    assert job.stages["embeddings"]["status"] == job.stages["links"]["status"] == "skipped"
    assert job.stages["summaries"] == {"status": "skipped"}

    events = runtime.run(_events(runtime, job_stream(job_id)))
    milestones = [(e["type"], e["stage"]) for e in events if e["type"] not in ("progress", "log")]
    assert milestones == [
        ("stage_started", "clone"),
        ("stage_done", "clone"),
        ("stage_started", "discover"),
        ("constellation", "discover"),  # the points found so far (all 5: one batch)
        ("stage_done", "discover"),
        ("unlock", "discover"),
        ("stage_started", "merkle"),
        ("stage_done", "merkle"),
        ("stage_started", "parse"),
        ("stage_done", "parse"),
        ("stage_started", "symbols"),
        ("stage_done", "symbols"),
        ("unlock", "symbols"),
        ("stage_started", "chunks"),
        ("stage_done", "chunks"),
        ("unlock", "chunks"),
        ("stage_started", "finalize"),
        ("stage_done", "finalize"),
        ("done", "finalize"),
    ]
    unlocks = [e["unlocks"] for e in events if e["type"] == "unlock"]
    assert unlocks == [["code_browser"], ["goto_definition"], ["keyword_search"]]
    progress = [e for e in events if e["type"] == "progress"]
    assert progress
    assert all(e["done"] <= e["total"] for e in progress)
    logs = [e["message"] for e in events if e["type"] == "log"]
    assert any(m.startswith("Checked out") for m in logs)
    assert any(m.startswith("Found 5 files") for m in logs)
    assert "Parsed 1 of 1 source files." in logs
    assert "Found 4 definitions. Go to definition is ready." in logs
    assert "File summaries are off: this server has no Anthropic API key." in logs
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


def test_cancelling_before_finalize_keeps_the_last_index(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, repo_id, job_id = ids
    real_merkle = build_merkle.run

    async def cancel() -> None:
        async with runtime.system_database.session() as db:
            job = await db.scalar(select(IndexJob).where(IndexJob.id == job_id).with_for_update())
            repo = await db.get(Repo, repo_id)
            assert job is not None
            assert repo is not None
            IndexJobStateMachine(job).cancel()
            settle_repo(repo, JobStatus.CANCELLED)

    def merkle_then_cancel(*args: Any, **kwargs: Any) -> Any:
        result = real_merkle(*args, **kwargs)
        runtime.run(cancel())  # the admin cancels just before finalize picks it up
        return result

    monkeypatch.setattr(build_merkle, "run", merkle_then_cancel)
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.CANCELLED
    assert job.stages["merkle"]["status"] == "done"
    assert "finalize" not in job.stages  # finalize saw the cancel and did nothing
    assert (repo.status, repo.last_indexed_sha) == (RepoStatus.PENDING, None)


def test_the_parse_and_symbols_stages_save_the_snapshots_definitions(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    details = job.stages["parse"]["details"]
    # src/app.py is the one file with a grammar (README.md, run.sh and the rest have none).
    assert (details["files"], details["parsed"], details["statuses"]) == (1, 1, {"parsed": 1})
    assert details["captures"]["calls"] == 3  # print(...), Greeter(), .greet(...)
    assert (details["symbols"], details["symbol_errors"]) == (4, 0)
    symbols = job.stages["symbols"]
    assert (symbols["status"], symbols["details"]["symbols"]) == ("done", 4)
    assert symbols["details"]["kinds"] == {"class": 1, "function": 1, "method": 1, "module": 1}
    assert job.snapshot_id is not None

    snapshot, nodes = runtime.run(_nodes(runtime.system_database, job.snapshot_id))
    assert snapshot.symbol_count == len(nodes) == 4
    assert [(n.symbol_key, n.kind, n.start_line, n.end_line) for n in nodes] == [
        ("py src/app.py/", "module", 1, 7),
        ("py src/app.py/Greeter#", "class", 1, 3),
        ("py src/app.py/Greeter#greet().", "method", 2, 3),
        ("py src/app.py/main().", "function", 6, 7),
    ]
    assert {n.org_id for n in nodes} == {org_id}
    assert {n.lang for n in nodes} == {"python"}


async def _chunks(
    database: Database, snapshot_id: uuid.UUID
) -> tuple[Snapshot, list[tuple[str, str, int, int, str | None, bool]]]:
    async with database.session() as db:
        snapshot = await db.get(Snapshot, snapshot_id)
        assert snapshot is not None
        rows = await db.execute(
            select(
                CodeChunk.path,
                CodeChunk.kind,
                CodeChunk.start_line,
                CodeChunk.end_line,
                CodeChunk.symbol_key,
                CodeChunk.symbol_id.is_not(None),
            ).where(CodeChunk.snapshot_id == snapshot_id)
        )
        return snapshot, sorted((p, k, s, e, key, linked) for p, k, s, e, key, linked in rows)


async def _partition(database: Database, repo_id: uuid.UUID) -> str:
    async with database.session() as db:
        found = await db.scalar(
            text("SELECT DISTINCT tableoid::regclass::text FROM chunks WHERE repo_id = :r"),
            {"r": repo_id},
        )
        return str(found)


def test_the_chunks_stage_saves_the_snapshots_chunks(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.snapshot_id is not None
    snapshot, chunks = runtime.run(_chunks(runtime.system_database, job.snapshot_id))
    # model.bin is an LFS pointer: never read, so not chunked.
    assert chunks == [
        (".gitattributes", "text", 1, 1, None, False),
        ("README.md", "doc", 1, 1, None, False),
        ("run.sh", "text", 1, 2, None, False),
        ("src/app.py", "class", 1, 3, "py src/app.py/Greeter#", True),
        ("src/app.py", "function", 6, 7, "py src/app.py/main().", True),
    ]
    assert snapshot.chunk_count == len(chunks)
    # The partition the stage vacuumed afterwards is the one holding them.
    assert runtime.run(vacuum_chunks(repo_id)) == runtime.run(
        _partition(runtime.system_database, repo_id)
    )
    details = job.stages["chunks"]["details"]
    assert (details["files"], details["chunked"], details["failed"]) == (4, 4, 0)
    assert details["chunks"] == len(chunks)
    assert details["kinds"] == {"class": 1, "doc": 1, "function": 1, "text": 2}
    events = runtime.run(_events(runtime, job_stream(job_id)))
    logs = [e["message"] for e in events if e["type"] == "log"]
    assert "Cut 4 files into 5 chunks. Keyword search is ready." in logs


async def _chunks_stage_again(database: Database, job_id: uuid.UUID) -> None:
    """Put a finished job back at a running chunks stage (a retry of that stage)."""
    async with database.session() as db:
        job = await db.get(IndexJob, job_id)
        assert job is not None
        job.status, job.stage = JobStatus.RUNNING, Stage.CHUNKS
        job.stages = {**job.stages, "chunks": {**job.stages["chunks"], "status": "running"}}


async def _seen(database: Database, snapshot_id: uuid.UUID) -> tuple[int | None, int]:
    """What search sees of the snapshot: its chunk count and its chunks."""
    async with database.session() as db:
        snapshot = await db.get(Snapshot, snapshot_id)
        assert snapshot is not None
        rows = await db.scalar(
            select(func.count()).select_from(CodeChunk).where(CodeChunk.snapshot_id == snapshot_id)
        )
        return snapshot.chunk_count, int(rows or 0)


@pytest.mark.parametrize("fails", [False, True])
def test_chunking_a_snapshot_again_serves_its_chunks_until_the_new_ones_commit(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)
    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    snapshot_id = job.snapshot_id
    assert snapshot_id is not None
    before = runtime.run(_seen(runtime.system_database, snapshot_id))
    assert before == (5, 5)

    seen: list[tuple[int | None, int]] = []

    class Watched(ChunkWriter):
        async def add(self, file: FileChunks) -> None:
            await super().add(file)
            seen.append(await _seen(runtime.system_database, snapshot_id))
            if fails:
                raise RuntimeError("the worker went away")

    monkeypatch.setattr(chunk_task, "ChunkWriter", Watched)
    runtime.run(_chunks_stage_again(runtime.system_database, job_id))
    kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    if fails:
        with pytest.raises(RuntimeError):
            chunk_task.chunk_snapshot.run(**kwargs)
    else:
        assert chunk_task.chunk_snapshot.run(**kwargs) == 5

    # Mid-run, the old chunks were still there to search; after, the new ones (or, if the run
    # failed, still the old ones).
    assert seen
    assert set(seen) == {before}
    assert runtime.run(_seen(runtime.system_database, snapshot_id)) == before


async def _another_job(database: Database, org_id: uuid.UUID, repo_id: uuid.UUID) -> uuid.UUID:
    async with database.session() as db:
        job = IndexJob(org_id=org_id, repo_id=repo_id, kind=JobKind.FULL)
        db.add(job)
        await db.flush()
        return job.id


def test_indexing_another_commit_prunes_the_last_ones_chunks(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)
    _, first_job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    old = first_job.snapshot_id
    assert old is not None
    assert runtime.run(_seen(runtime.system_database, old)) == (5, 5)

    # The branch moves back to the first commit: a new snapshot, indexed in full.
    github.get(COMMITS_URL).mock(return_value=httpx.Response(200, json={"sha": bare_repo.first}))
    next_job = runtime.run(_another_job(runtime.system_database, org_id, repo_id))
    _run(org_id, repo_id, next_job)
    repo, job = runtime.run(_state(runtime.system_database, repo_id, next_job))
    assert (job.status, repo.last_indexed_sha) == (JobStatus.SUCCEEDED, bare_repo.first)
    new = job.snapshot_id
    assert new is not None
    assert new != old
    count, rows = runtime.run(_seen(runtime.system_database, new))
    assert count == rows > 0
    # The superseded snapshot isn't searchable any more, and its chunks are gone.
    assert runtime.run(_seen(runtime.system_database, old)) == (None, 0)


async def _searched(
    database: Database, repo_id: uuid.UUID, snapshot_id: uuid.UUID
) -> tuple[dict[str, uuid.UUID], list[str | None]]:
    """The snapshot's file ids, and what keyword search finds in it for the greeter."""
    async with database.session() as db:
        files = await file_ids(db, snapshot_id)
        hits = await keyword_search(db, repo_id=repo_id, snapshot_id=snapshot_id, query="greet")
        return files, [hit.symbol_key for hit in hits]


def test_reindexing_the_same_commit_keeps_serving_its_search(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)
    _, first = runtime.run(_state(runtime.system_database, repo_id, job_id))
    snapshot_id = first.snapshot_id
    assert snapshot_id is not None
    files, found = runtime.run(_searched(runtime.system_database, repo_id, snapshot_id))
    assert found

    # Search after every stage of a reindex of the same commit (the same snapshot).
    seen: list[tuple[str, tuple[dict[str, uuid.UUID], list[str | None]]]] = []

    def after(sender: Any = None, **_: Any) -> None:
        name = getattr(sender, "name", "")
        seen.append((name, runtime.run(_searched(runtime.system_database, repo_id, snapshot_id))))

    task_postrun.connect(after)
    try:
        again = runtime.run(_another_job(runtime.system_database, org_id, repo_id))
        _run(org_id, repo_id, again)
    finally:
        task_postrun.disconnect(after)
    _, job = runtime.run(_state(runtime.system_database, repo_id, again))
    assert (job.status, job.snapshot_id) == (JobStatus.SUCCEEDED, snapshot_id)
    stages = {TaskName(name) for name, _ in seen if name in set(TaskName)}
    assert {TaskName.DISCOVER_FILES, TaskName.PARSE_FILES, TaskName.CHUNK_FILES} <= stages
    assert [state for _, state in seen] == [(files, found)] * len(seen)


def test_a_chain_sent_without_the_chunks_stage_gets_it_from_finalize(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    """A job whose chain was sent before the deploy that added the chunks (and summaries)
    stages."""
    org_id, repo_id, job_id = ids
    kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    later = {chunk_task.chunk_snapshot, summarize_snapshot}
    old = [stage for stage in orchestrate.STAGES if stage not in later]
    chain(*(stage.si(**kwargs) for stage in old)).apply().get()

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    assert job.stages["chunks"]["status"] == "done"
    assert job.snapshot_id is not None
    assert runtime.run(_seen(runtime.system_database, job.snapshot_id)) == (5, 5)


# ---------------------------------------------------------------- summaries

BATCHES = "/v1/messages/batches"
USAGE = {
    "input_tokens": 2_000,
    "output_tokens": 300,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}


def _answer(params: dict[str, Any]) -> dict[str, Any]:
    """A summary of the file the request shows, one for every symbol it asks about."""
    prompt: str = params["messages"][0]["content"]
    handles = re.findall(r"^(s\d+)\t\w+ (\w+)", prompt, flags=re.MULTILINE)
    return {
        "file_summary": "Greets people by name from the command line.",
        "symbols": [
            {"key": handle, "summary": f"{name} greets.", "domain_terms": ["greeting"]}
            for handle, name in handles
        ],
    }


def _single(request: httpx2.Request) -> httpx2.Response:
    """A call at once: a file's summary again (a retry), or a directory's."""
    body = json.loads(request.content)
    if "<path>" in body["messages"][0]["content"]:
        return httpx2.Response(200, json=message(json.dumps(_answer(body)), usage=USAGE))
    return httpx2.Response(200, json=message(json.dumps({"summary": "The command-line greeter."})))


@pytest.fixture
def claude() -> FakeAnthropic:
    """A batch API that ends on its first poll and answers every request, and directory
    summaries one call at a time."""
    api = FakeAnthropic()

    def submitted() -> list[dict[str, Any]]:
        return api.bodies(BATCHES)[0]["requests"]

    def results(_: httpx2.Request) -> httpx2.Response:
        lines = [
            {
                "custom_id": r["custom_id"],
                "result": {
                    "type": "succeeded",
                    "message": message(json.dumps(_answer(r["params"])), usage=USAGE),
                },
            }
            for r in submitted()
        ]
        return jsonl(*lines)

    def state(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=batch("msgbatch_1", "ended", succeeded=len(submitted())))

    api.routes.update(
        {
            BATCHES: httpx2.Response(200, json=batch("msgbatch_1")),
            f"{BATCHES}/msgbatch_1": state,
            f"{BATCHES}/msgbatch_1/results": results,
            "/v1/messages": _single,
            "/v1/messages/count_tokens": httpx2.Response(200, json={"input_tokens": 2_000}),
        }
    )
    return api


async def _summaries(
    runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID, sha: str
) -> tuple[Any, list[GlossaryTerm], list[tuple[bool, int]]]:
    storage = await runtime.storage()
    found = await storage.get_bytes(
        Bucket.ARTIFACTS, artifact_key(org_id, repo_id, sha, "summaries.json"), max_bytes=1 << 20
    )
    async with runtime.system_database.session() as db:
        terms = list(await db.scalars(select(GlossaryTerm).where(GlossaryTerm.repo_id == repo_id)))
        usage = [
            (bool(batched), int(count))
            for batched, count in await db.execute(
                select(UsageEvent.batch, func.count())
                .where(UsageEvent.org_id == org_id, UsageEvent.ref_id == str(job_id))
                .group_by(UsageEvent.batch)
                .order_by(UsageEvent.batch)
            )
        ]
    return (json.loads(found) if found is not None else None), terms, usage


def test_files_are_summarized_and_the_wiki_unlocked(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    stage = job.stages["summaries"]
    assert (stage["status"], stage["done"], stage["total"]) == ("done", 1, 1)
    details = stage["details"]
    assert (details["files"], details["summarized"], details["skipped"]) == (1, 1, 0)
    assert (details["phase"], details["directories"]) == ("collected", 2)  # src/ and the root
    assert Decimal(details["estimate_usd"]) > 0
    # One batch of the one file with symbols, then one call: the root takes src/'s summary.
    [sent] = claude.bodies(BATCHES)
    assert len(sent["requests"]) == 1
    assert len(claude.bodies("/v1/messages")) == 1

    artifact, terms, usage = runtime.run(
        _summaries(runtime, org_id, repo_id, job_id, bare_repo.second)
    )
    [file] = artifact["files"]
    assert file["path"] == "src/app.py"
    assert {s["key"] for s in file["symbols"]} == {
        "py src/app.py/Greeter#", "py src/app.py/Greeter#greet().", "py src/app.py/main().",
    }  # fmt: skip
    assert [(t.term, len(t.symbol_ids)) for t in terms] == [("greeting", 3)]
    assert usage == [(False, 1), (True, 1)]  # the rollup call, and the batch's one answer

    events = runtime.run(_events(runtime, job_stream(job_id)))
    milestones = [(e["type"], e["stage"]) for e in events if e["type"] not in ("progress", "log")]
    start = milestones.index(("stage_started", "summaries"))
    assert milestones[start : start + 4] == [
        ("stage_started", "summaries"),
        ("stage_done", "summaries"),
        ("unlock", "summaries"),
        ("stage_started", "finalize"),
    ]
    assert [e["unlocks"] for e in events if e["type"] == "unlock"][-1] == ["wiki"]
    progress = [(e["done"], e["total"]) for e in events if e["type"] == "progress"]
    assert (0, 1) in progress
    assert (1, 1) in progress
    logs = [e["message"] for e in events if e["type"] == "log"]
    assert "Summarizing 1 files in one batch (under $0.01 with the directory summaries)." in logs
    assert "Summarized 1 files and 2 directories." in logs


@pytest.mark.parametrize("cap", [Decimal(0)])
def test_over_the_cost_cap_summaries_are_skipped_and_the_index_still_finishes(
    cap: Decimal,
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    stage = job.stages["summaries"]
    assert stage["status"] == "skipped"
    assert (stage["details"]["reason"], stage["details"]["cap_usd"]) == ("cost_cap", "0")
    assert claude.bodies(BATCHES) == []  # nothing sent, nothing reserved
    assert claude.bodies("/v1/messages") == []
    events = runtime.run(_events(runtime, job_stream(job_id)))
    assert ["wiki"] not in [e.get("unlocks") for e in events if e["type"] == "unlock"]
    warned = [e["message"] for e in events if e["type"] == "log" and e["level"] == "warn"]
    assert (
        "File summaries skipped: they would cost under $0.01, and the organization has"
        " used $0.00 of its $0.00 monthly budget." in warned
    )


def test_a_collection_that_meets_an_outage_resumes_without_sending_or_billing_twice(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    results = claude.routes[f"{BATCHES}/msgbatch_1/results"]
    outages = [httpx2.Response(529, json={"type": "error", "error": {
        "type": "overloaded_error", "message": "Overloaded"}})]  # fmt: skip

    def flaky(request: httpx2.Request) -> httpx2.Response:
        if outages:
            return outages.pop()
        assert callable(results)
        answer = results(request)
        assert isinstance(answer, httpx2.Response)
        return answer

    claude.routes[f"{BATCHES}/msgbatch_1/results"] = flaky
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.SUCCEEDED
    stage = job.stages["summaries"]
    assert (stage["status"], stage["details"]["unavailable"]) == ("done", 1)
    assert stage["details"]["summarized"] == 1
    assert len(claude.bodies(BATCHES)) == 1  # sent once
    artifact, _, usage = runtime.run(_summaries(runtime, org_id, repo_id, job_id, bare_repo.second))
    assert [f["path"] for f in artifact["files"]] == ["src/app.py"]
    assert usage == [(False, 1), (True, 1)]  # the answer billed once


def test_a_file_whose_answer_is_unusable_is_retried_once_at_full_price(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    _garbled(claude)
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    stage = job.stages["summaries"]
    assert stage["status"] == "done"
    details = stage["details"]
    assert (details["phase"], details["retried"], details["summarized"], details["skipped"]) == (
        "retried", 1, 1, 0,
    )  # fmt: skip
    asked = [b["messages"][0]["content"] for b in claude.bodies("/v1/messages")]
    assert sum("<path>src/app.py</path>" in prompt for prompt in asked) == 1  # one retry
    artifact, terms, usage = runtime.run(
        _summaries(runtime, org_id, repo_id, job_id, bare_repo.second)
    )
    assert artifact["skipped"] == {}
    assert [t.term for t in terms] == ["greeting"]
    # The batch's unusable answer (billed), then the retry and the directory call at full price.
    assert usage == [(False, 2), (True, 1)]


def _garbled(claude: FakeAnthropic) -> None:
    """The batch answers its one file with JSON cut short."""

    def results(_: httpx2.Request) -> httpx2.Response:
        [request] = claude.bodies(BATCHES)[0]["requests"]
        answer = message('{"file_summary": "cut', usage=USAGE)
        result = {"type": "succeeded", "message": answer}
        return jsonl({"custom_id": request["custom_id"], "result": result})

    claude.routes[f"{BATCHES}/msgbatch_1/results"] = results


def _serve_next_batch(claude: FakeAnthropic, batch_id: str) -> None:
    """The next batch sent is ``batch_id``: it ends on its first poll, every answer usable."""

    def submitted() -> list[dict[str, Any]]:
        return claude.bodies(BATCHES)[-1]["requests"]

    def results(_: httpx2.Request) -> httpx2.Response:
        return jsonl(*(
            {"custom_id": r["custom_id"], "result": {"type": "succeeded",
             "message": message(json.dumps(_answer(r["params"])), usage=USAGE)}}
            for r in submitted()
        ))  # fmt: skip

    def state(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=batch(batch_id, "ended", succeeded=len(submitted())))

    claude.routes.update(
        {
            BATCHES: httpx2.Response(200, json=batch(batch_id)),
            f"{BATCHES}/{batch_id}": state,
            f"{BATCHES}/{batch_id}/results": results,
        }
    )


async def _next_job(database: Database, org_id: uuid.UUID, repo_id: uuid.UUID) -> uuid.UUID:
    async with database.session() as db:
        job = IndexJob(org_id=org_id, repo_id=repo_id, kind=JobKind.FULL)
        db.add(job)
        await db.flush()
        return job.id


def test_retries_stopped_part_way_are_not_sent_again(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task stops after the retries were paid for, before they were stored: run again, it
    doesn't send them again. The file stays without a summary, the marker at "retrying"; the
    next index summarizes it."""
    org_id, repo_id, job_id = ids
    _garbled(claude)
    real = summaries.retry_failed
    stops = [RetryableError("the worker went away")]

    async def stopped(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        if stops:
            raise stops.pop()

    monkeypatch.setattr("sensei_workers.tasks.summaries.retry_failed", stopped)
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.SUCCEEDED
    stage = job.stages["summaries"]
    assert (stage["status"], stage["details"]["reason"]) == ("skipped", "nothing_summarized")
    assert stage["details"]["phase"] == "retrying"
    asked = [b["messages"][0]["content"] for b in claude.bodies("/v1/messages")]
    assert sum("<path>src/app.py</path>" in prompt for prompt in asked) == 1  # retried once
    artifact, _, _ = runtime.run(_summaries(runtime, org_id, repo_id, job_id, bare_repo.second))
    assert artifact["files"] == []
    assert list(artifact["skipped"]) == ["src/app.py"]

    # The next index fills the gap.
    _serve_next_batch(claude, "msgbatch_2")
    later = runtime.run(_next_job(runtime.system_database, org_id, repo_id))
    _run(org_id, repo_id, later)
    _, job = runtime.run(_state(runtime.system_database, repo_id, later))
    assert job.stages["summaries"]["status"] == "done"
    assert "partial" not in job.stages["summaries"]["details"]
    artifact, _, _ = runtime.run(_summaries(runtime, org_id, repo_id, later, bare_repo.second))
    assert [f["path"] for f in artifact["files"]] == ["src/app.py"]


def test_directory_summaries_stopped_part_way_are_not_sent_again(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, repo_id, job_id = ids
    real = rollups.summarize_directories
    stops = [RetryableError("the worker went away")]

    async def stopped(*args: Any, **kwargs: Any) -> Any:
        found = await real(*args, **kwargs)
        if stops:
            raise stops.pop()
        return found

    monkeypatch.setattr(rollups, "summarize_directories", stopped)
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.SUCCEEDED
    stage = job.stages["summaries"]
    assert stage["status"] == "done"
    details = stage["details"]
    assert (details["rollups"], details["directories"]) == ("started", 0)
    assert details["partial"] == {"directories": True}
    assert len(claude.bodies("/v1/messages")) == 1  # the directory call, made once
    assert runtime.run(_rollups_artifact(runtime, org_id, repo_id, bare_repo.second)) is None
    events = runtime.run(_events(runtime, job_stream(job_id)))
    warned = [e["message"] for e in events if e["type"] == "log" and e["level"] == "warn"]
    assert (
        "Summaries are partial: the directory summaries stopped part way and weren't paid for"
        " twice; the next index summarizes them again." in warned
    )

    # The next index fills the gap.
    _serve_next_batch(claude, "msgbatch_2")
    later = runtime.run(_next_job(runtime.system_database, org_id, repo_id))
    _run(org_id, repo_id, later)
    _, job = runtime.run(_state(runtime.system_database, repo_id, later))
    details = job.stages["summaries"]["details"]
    assert (details["rollups"], details["directories"]) == ("written", 2)
    assert "partial" not in details
    found = runtime.run(_rollups_artifact(runtime, org_id, repo_id, bare_repo.second))
    assert found is not None
    assert len(found["directories"]) == 2


async def _rollups_artifact(
    runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID, sha: str
) -> Any:
    storage = await runtime.storage()
    key = artifact_key(org_id, repo_id, sha, "rollups.json")
    found = await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=1 << 20)
    return json.loads(found) if found is not None else None
