"""A whole index run through the chain (eager): stages in order, events on the job's stream,
the repository ready at the commit; file summaries through a fake Claude (within the cap, over
it, and resumed after an outage); and cancelling while the clone runs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
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
from sqlalchemy import inspect as sa_inspect
from uuid_utils.compat import uuid7

from sensei_core.config import Settings
from sensei_core.db import Database
from sensei_core.errors import NotFoundError
from sensei_core.indexing import pushes
from sensei_core.indexing.events import JobOutbox, job_stream, org_stream
from sensei_core.indexing.jobs import settle_repo
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.jobs import TaskName
from sensei_core.models import (
    SYMBOL_KINDS,
    CodeChunk,
    CodeEdge,
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
    UsageKind,
)
from sensei_core.storage import Bucket, artifact_key, repo_prefix
from sensei_engine.chunk.store import ChunkWriter, FileChunks, make_live, new_generation
from sensei_engine.embed.provider import EmbeddingUsage, InputType, UsageSink
from sensei_engine.embed.voyage import VoyageError
from sensei_engine.graph import stage as graph_stage
from sensei_engine.graph.snapshot import ARTIFACT_NAME, GraphSnapshot
from sensei_engine.graph.trace import trace_feature
from sensei_engine.ingest.store import file_ids
from sensei_engine.link.fastapi import Route, RuntimeRoutes
from sensei_engine.llm import rollups, summaries
from sensei_engine.search.bm25 import chunk_partition, keyword_search
from sensei_testing.anthropic import FakeAnthropic, batch, jsonl, message
from sensei_workers import statistics
from sensei_workers.base import NotYetError, RetryableError
from sensei_workers.ingest.git import GitRunner
from sensei_workers.runtime import Runtime, set_runtime
from sensei_workers.tasks import chunk as chunk_task
from sensei_workers.tasks import embed as embed_task
from sensei_workers.tasks import graph as graph_task
from sensei_workers.tasks import links as links_task
from sensei_workers.tasks import maintenance, orchestrate
from sensei_workers.tasks.merkle import build_merkle
from sensei_workers.tasks.orchestrate import run_index_job
from sensei_workers.tasks.pushes import Outcome, begin, start_push_index
from sensei_workers.tasks.summaries import summarize_snapshot

pytestmark = pytest.mark.integration

COMMITS_URL = "https://api.github.com/repos/acme-gh/demo/commits/main"


@pytest.fixture
def runtime(
    migrated_test_db: Settings, bare_repo: Any, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Runtime]:
    """The workers' runtime; with Claude (the ``claude`` fixture, and ``cap``) and Voyage (the
    ``voyage`` fixture) when the test asks for them, else without (summaries and embeddings are
    passed over)."""
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
    voyage: FakeVoyage | None = (
        request.getfixturevalue("voyage") if "voyage" in request.fixturenames else None
    )
    current = Runtime(
        settings, anthropic=claude.client() if claude is not None else None, embeddings=voyage
    )
    set_runtime(current)
    yield current
    set_runtime(None)


class FakeVoyage:
    """Embeds each text as a unit vector picked by its hash, billing a token per 4 characters;
    or refuses every request with ``refusal``. Runs ``before``'s hooks first, one a request."""

    provider = "voyage"
    model = "voyage-code-4"
    dims = 1024

    def __init__(self) -> None:
        self.calls: list[tuple[int, InputType]] = []  # texts per request, and what for
        self.refusal: VoyageError | None = None
        self.before: list[Callable[[], Awaitable[None]]] = []  # one each, before a request

    async def embed(
        self, texts: Sequence[str], input_type: InputType, *, on_usage: UsageSink | None = None
    ) -> list[list[float]]:
        if self.before:
            await self.before.pop(0)()
        if self.refusal is not None:
            raise self.refusal
        self.calls.append((len(texts), input_type))
        if on_usage is not None:
            tokens = sum(len(t) // 4 + 1 for t in texts)
            await on_usage(EmbeddingUsage(self.provider, self.model, len(texts), tokens))
        vectors: list[list[float]] = []
        for t in texts:
            vector = [0.0] * self.dims
            vector[int(hashlib.sha256(t.encode()).hexdigest(), 16) % self.dims] = 1.0
            vectors.append(vector)
        return vectors


@pytest.fixture
def voyage() -> FakeVoyage:
    return FakeVoyage()


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
    assert {job.stages[s]["status"] for s in ("parse", "symbols", "chunks", "graph")} == {"done"}
    graph = job.stages["graph"]["details"]
    assert (graph["files"], graph["extracted"], graph["edges"]) == (1, 1, 2)
    assert graph["resolvers"]["python"]["imports"]["unresolved"] == 0
    # No Voyage or Anthropic key: embeddings and summaries are recorded as skipped. The links ran
    # (a Python file, nothing in it to link).
    assert job.stages["embeddings"]["status"] == "skipped"
    links = job.stages["links"]
    assert links["status"] == "done"
    assert (links["details"]["files"], links["details"]["edges"]) == (1, 0)
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
        ("stage_started", "graph"),
        ("stage_done", "graph"),
        ("unlock", "graph"),
        ("stage_started", "links"),
        ("stage_done", "links"),
        ("unlock", "links"),
        ("stage_started", "finalize"),
        ("stage_done", "finalize"),
        ("done", "finalize"),
    ]
    unlocks = [e["unlocks"] for e in events if e["type"] == "unlock"]
    assert unlocks == [
        ["code_browser"], ["goto_definition"], ["keyword_search", "ask"], ["graph_view", "callers"],
        ["trace", "diagrams", "fixes", "changes"],
    ]  # fmt: skip
    progress = [e for e in events if e["type"] == "progress"]
    assert progress
    assert all(e["done"] <= e["total"] for e in progress)
    logs = [e["message"] for e in events if e["type"] == "log"]
    assert any(m.startswith("Checked out") for m in logs)
    assert any(m.startswith("Found 5 files") for m in logs)
    assert "Parsed 1 of 1 source files." in logs
    assert "Found 4 definitions. Go to definition is ready." in logs
    assert any(m.startswith("Linked 1 files: ") for m in logs)
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


@pytest.fixture
def upkeep(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[uuid.UUID]]:
    """The repositories the stages asked to analyze, and to vacuum (still done, in place)."""
    calls: dict[str, list[uuid.UUID]] = {"analyze": [], "vacuum": []}
    analyze, vacuum = statistics.analyze_chunks, maintenance.request_vacuum

    async def analyzed(repo_id: uuid.UUID) -> None:
        calls["analyze"].append(repo_id)
        await analyze(repo_id)

    def vacuumed(repo_id: uuid.UUID, *, eager: bool = False) -> bool:
        del eager  # in place: nothing goes to a broker other workers may share
        calls["vacuum"].append(repo_id)
        return vacuum(repo_id, eager=True)

    monkeypatch.setattr(chunk_task, "analyze_chunks", analyzed)
    monkeypatch.setattr(chunk_task, "request_vacuum", vacuumed)
    monkeypatch.setattr(orchestrate, "request_vacuum", vacuumed)
    monkeypatch.setattr(embed_task, "request_vacuum", vacuumed)
    return calls


def test_the_chunks_stage_saves_the_snapshots_chunks(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    upkeep: dict[str, list[uuid.UUID]],
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
    assert snapshot.chunk_generation is not None
    # Rows written, none deleted: the repository's partition analyzed, not vacuumed.
    assert upkeep == {"analyze": [repo_id], "vacuum": []}
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
    """What search sees of the snapshot: its chunk count and its live generation's chunks."""
    async with database.session() as db:
        snapshot = await db.get(Snapshot, snapshot_id)
        assert snapshot is not None
        rows = await db.scalar(
            select(func.count())
            .select_from(CodeChunk)
            .where(
                CodeChunk.snapshot_id == snapshot_id,
                CodeChunk.generation == snapshot.chunk_generation,
            )
        )
        return snapshot.chunk_count, int(rows or 0)


async def _chunk_ids(database: Database, snapshot_id: uuid.UUID) -> set[uuid.UUID]:
    """Every chunk row of the snapshot, of any generation."""
    async with database.session() as db:
        found = await db.scalars(select(CodeChunk.id).where(CodeChunk.snapshot_id == snapshot_id))
        return set(found)


async def _lock_chunks(database: Database) -> None:
    """Take the lock DDL on ``chunks`` takes, without waiting (raises if it's held)."""
    async with database.session() as db:
        await db.execute(text("LOCK TABLE chunks IN ACCESS EXCLUSIVE MODE NOWAIT"))
        await db.rollback()


@pytest.mark.parametrize("fails", [False, True])
def test_chunking_a_snapshot_again_serves_its_chunks_until_the_new_ones_are_live(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    upkeep: dict[str, list[uuid.UUID]],
    fails: bool,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)
    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    snapshot_id = job.snapshot_id
    assert snapshot_id is not None
    before = runtime.run(_seen(runtime.system_database, snapshot_id))
    assert before == (5, 5)
    old = runtime.run(_chunk_ids(runtime.system_database, snapshot_id))

    seen: list[tuple[int | None, int]] = []
    written: list[int] = []

    class Watched(ChunkWriter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **{**kwargs, "batch_rows": 1})  # every chunk committed

        async def add(self, file: FileChunks) -> None:
            await super().add(file)
            written.append(self.written)
            seen.append(await _seen(runtime.system_database, snapshot_id))
            # Between batches no transaction is open: DDL on chunks doesn't wait on the stage.
            await _lock_chunks(runtime.system_database)
            if fails and self.written >= 3:
                raise RuntimeError("the worker went away")

    monkeypatch.setattr(chunk_task, "ChunkWriter", Watched)
    runtime.run(_chunks_stage_again(runtime.system_database, job_id))
    upkeep["analyze"].clear()
    kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    if fails:
        with pytest.raises(RuntimeError):
            chunk_task.chunk_snapshot.run(**kwargs)
    else:
        assert chunk_task.chunk_snapshot.run(**kwargs) == 5

    # Mid-run, new rows were committed, and search still saw the old ones only.
    assert written[-1] >= 3
    assert seen
    assert set(seen) == {before}
    assert runtime.run(_seen(runtime.system_database, snapshot_id)) == before
    rows = runtime.run(_chunk_ids(runtime.system_database, snapshot_id))
    if fails:
        # The live generation as it was, and nothing of the stopped run's left.
        assert rows == old
    else:
        assert rows.isdisjoint(old)  # the new generation's, the old one's deleted
    # Rows were deleted either way: the partition is vacuumed.
    assert upkeep == {"analyze": [], "vacuum": [repo_id]}


def test_a_run_killed_before_it_discarded_its_rows_is_swept_by_the_next(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    upkeep: dict[str, list[uuid.UUID]],
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)
    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    snapshot_id = job.snapshot_id
    assert snapshot_id is not None
    old = runtime.run(_chunk_ids(runtime.system_database, snapshot_id))

    class Killed(ChunkWriter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **{**kwargs, "batch_rows": 1})

        async def add(self, file: FileChunks) -> None:
            await super().add(file)
            if self.written >= 2:
                raise RuntimeError("killed")

    async def unreachable(*_: object) -> int:
        raise ConnectionError("the database went away")

    with monkeypatch.context() as killed:
        killed.setattr(chunk_task, "ChunkWriter", Killed)
        killed.setattr(chunk_task, "_discard", unreachable)
        runtime.run(_chunks_stage_again(runtime.system_database, job_id))
        kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
        with pytest.raises(RuntimeError):
            chunk_task.chunk_snapshot.run(**kwargs)
    left = runtime.run(_chunk_ids(runtime.system_database, snapshot_id)) - old
    assert len(left) >= 2  # the killed run's rows, not searched but still there

    runtime.run(_chunks_stage_again(runtime.system_database, job_id))
    upkeep["analyze"].clear()
    upkeep["vacuum"].clear()
    assert chunk_task.chunk_snapshot.run(**kwargs) == 5
    rows = runtime.run(_chunk_ids(runtime.system_database, snapshot_id))
    assert len(rows) == 5
    assert rows.isdisjoint(old | left)
    # Swept as the run started, and the old generation deleted as it went live.
    assert upkeep == {"analyze": [], "vacuum": [repo_id, repo_id]}


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
    upkeep: dict[str, list[uuid.UUID]],
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
    assert runtime.run(_chunk_ids(runtime.system_database, old)) == set()
    # Each snapshot's first chunking only wrote rows; finalize's prune deleted some.
    assert upkeep == {"analyze": [repo_id, repo_id], "vacuum": [repo_id]}


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


def test_a_worker_finalizing_such_a_job_sends_the_chunks_stage_once(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not in place: finalize sends the stages from chunks on and waits for them; a second
    delivery doesn't send them again; a send that failed is tried again by the next one."""
    org_id, repo_id, job_id = ids
    kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    before = orchestrate.STAGES[: orchestrate.STAGES.index(chunk_task.chunk_snapshot)]
    chain(*(stage.si(**kwargs) for stage in before)).apply().get()
    sent: list[int] = []
    broken = [True]  # the first send fails

    def send_stages(*_: Any, eager: bool = False, first: int = 0) -> list[str]:
        assert not eager
        if broken:
            broken.clear()
            raise ConnectionError("the broker is down")
        sent.append(first)
        return []

    monkeypatch.setattr(orchestrate, "send_stages", send_stages)
    finalize = orchestrate.finalize_index
    with pytest.raises(ConnectionError):
        finalize.run(**kwargs)  # not sent: the key goes, so the next delivery sends
    with pytest.raises(NotYetError, match="was sent"):
        finalize.run(**kwargs)
    with pytest.raises(NotYetError, match="hasn't run"):
        finalize.run(**kwargs)  # a second delivery: sent once already
    assert sent == [orchestrate.STAGES.index(chunk_task.chunk_snapshot)]
    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, job.stage) == (JobStatus.RUNNING, Stage.SYMBOLS)


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

    def submitted(n: int) -> list[dict[str, Any]]:
        return api.bodies(BATCHES)[n - 1]["requests"]

    def results(n: int) -> Callable[[httpx2.Request], httpx2.Response]:
        def answer(_: httpx2.Request) -> httpx2.Response:
            lines = [
                {
                    "custom_id": r["custom_id"],
                    "result": {
                        "type": "succeeded",
                        "message": message(json.dumps(_answer(r["params"])), usage=USAGE),
                    },
                }
                for r in submitted(n)
            ]
            return jsonl(*lines)

        return answer

    def state(n: int) -> Callable[[httpx2.Request], httpx2.Response]:
        def answer(_: httpx2.Request) -> httpx2.Response:
            ended = batch(f"msgbatch_{n}", "ended", succeeded=len(submitted(n)))
            return httpx2.Response(200, json=ended)

        return answer

    def created(_: httpx2.Request) -> httpx2.Response:
        # A batch of its own for each run's (msgbatch_1, msgbatch_2, ...); a test's own routes
        # for one stay.
        n = len(api.bodies(BATCHES))
        api.routes.setdefault(f"{BATCHES}/msgbatch_{n}", state(n))
        api.routes.setdefault(f"{BATCHES}/msgbatch_{n}/results", results(n))
        return httpx2.Response(200, json=batch(f"msgbatch_{n}"))

    api.routes.update(
        {
            BATCHES: created,
            f"{BATCHES}/msgbatch_1": state(1),
            f"{BATCHES}/msgbatch_1/results": results(1),
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
        ("stage_started", "graph"),  # then the code graph
    ]
    assert [e["unlocks"] for e in events if e["type"] == "unlock"][-3:] == [
        ["wiki"], ["graph_view", "callers"], ["trace", "diagrams", "fixes", "changes"],
    ]  # fmt: skip
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
    """The task stops after a retry was paid for, before it was stored: run again, it doesn't
    send it again. The file stays without a summary, the marker at "retrying"; the next index
    summarizes it."""
    org_id, repo_id, job_id = ids
    _garbled(claude)
    real = summaries.retry_failed
    stops = [RetryableError("the worker went away")]

    async def stopped(*args: Any, **kwargs: Any) -> None:
        if stops:
            stop = stops.pop()

            async def crash(_: Any) -> None:
                raise stop

            kwargs["on_summary"] = crash  # answered and paid for, then the worker goes
        await real(*args, **kwargs)

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


def test_a_retry_stored_before_a_stop_is_kept(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each retried summary is stored as it comes: a stop after it keeps what was paid for."""
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
    stage = job.stages["summaries"]
    assert stage["status"] == "done"
    assert (stage["details"]["summarized"], stage["details"]["skipped"]) == (1, 0)
    assert "partial" not in stage["details"]
    asked = [b["messages"][0]["content"] for b in claude.bodies("/v1/messages")]
    assert sum("<path>src/app.py</path>" in prompt for prompt in asked) == 1  # retried once
    artifact, _, _ = runtime.run(_summaries(runtime, org_id, repo_id, job_id, bare_repo.second))
    assert [f["path"] for f in artifact["files"]] == ["src/app.py"]


def test_an_outage_before_the_retries_are_paid_for_retries_them_later(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    """Counting the retries' tokens (free) meets an outage: nothing was paid, so "retrying"
    isn't marked and the next attempt retries them."""
    org_id, repo_id, job_id = ids
    _garbled(claude)
    counted = claude.routes["/v1/messages/count_tokens"]
    calls: list[int] = []

    def flaky(_: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 2:  # the batch's count went through; the retries' first doesn't
            return httpx2.Response(529, json={"type": "error", "error": {
                "type": "overloaded_error", "message": "Overloaded"}})  # fmt: skip
        assert isinstance(counted, httpx2.Response)
        return counted

    claude.routes["/v1/messages/count_tokens"] = flaky
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    details = job.stages["summaries"]["details"]
    assert (details["phase"], details["summarized"], details["unavailable"]) == ("retried", 1, 1)
    assert "partial" not in details


def test_rollups_written_just_before_a_stop_are_kept(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task stops after writing rollups.json, before recording it: the artifact is this
    job's, so it counts, and nothing is sent again."""
    org_id, repo_id, job_id = ids
    storage = runtime.run(runtime.storage())
    real = storage.put_bytes
    stops = [RetryableError("the worker went away")]

    async def put_bytes(bucket: Bucket, key: str, data: bytes, **kwargs: Any) -> Any:
        written = await real(bucket, key, data, **kwargs)
        if key.endswith("rollups.json") and stops:
            raise stops.pop()
        return written

    monkeypatch.setattr(storage, "put_bytes", put_bytes)
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    details = job.stages["summaries"]["details"]
    assert (details["rollups"], details["directories"]) == ("written", 2)
    assert "partial" not in details
    assert len(claude.bodies("/v1/messages")) == 1  # the directory call, made once


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


# ---------------------------------------------------------------- embeddings


async def _embedding_state(
    runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID
) -> tuple[int, int, list[int], bool]:
    """Chunks with a vector and without; the job's embedding usage (tokens per request); and
    whether the repository's partition has its HNSW index."""
    async with runtime.system_database.session() as db:
        with_vector = await db.scalar(
            select(func.count())
            .select_from(CodeChunk)
            .where(CodeChunk.repo_id == repo_id, CodeChunk.embedding.is_not(None))
        )
        without = await db.scalar(
            select(func.count())
            .select_from(CodeChunk)
            .where(CodeChunk.repo_id == repo_id, CodeChunk.embedding.is_(None))
        )
        usage = list(
            await db.scalars(
                select(UsageEvent.tokens_in).where(
                    UsageEvent.org_id == org_id,
                    UsageEvent.ref_id == str(job_id),
                    UsageEvent.kind == UsageKind.EMBEDDING,
                )
            )
        )
        partition = await chunk_partition(db, repo_id)
        indexed = bool(
            await db.scalar(
                text("SELECT to_regclass(:name) IS NOT NULL"),
                {"name": f"{partition}_embedding_hnsw"},
            )
        )
    return int(with_vector or 0), int(without or 0), usage, indexed


def test_chunks_are_embedded_and_semantic_search_unlocked(
    voyage: FakeVoyage,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    upkeep: dict[str, list[uuid.UUID]],
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)
    # The vectors were written over every row: the partition is vacuumed (the parent table,
    # every repository's partitions, isn't analyzed).
    assert upkeep["vacuum"] == [repo_id]

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    stage = job.stages["embeddings"]
    chunks = job.stages["chunks"]["details"]["chunks"]
    assert (stage["status"], stage["done"], stage["total"]) == ("done", chunks, chunks)
    assert (stage["details"]["cached"], stage["details"]["embedded"]) == (0, chunks)
    assert {input_type for _, input_type in voyage.calls} == {InputType.DOCUMENT}
    with_vector, without, usage, indexed = runtime.run(
        _embedding_state(runtime, org_id, repo_id, job_id)
    )
    assert (with_vector, without) == (chunks, 0)
    assert len(usage) == len(voyage.calls)
    assert indexed
    # What the run was charged is on the job, by kind.
    finalized = job.stages["finalize"]["details"]
    assert job.cost_usd > 0
    assert finalized["cost_usd"] == str(job.cost_usd)
    assert set(finalized["costs"]) == {"embedding"}

    events = runtime.run(_events(runtime, job_stream(job_id)))
    unlocks = {(e["stage"], tuple(e["unlocks"])) for e in events if e["type"] == "unlock"}
    assert ("embeddings", ("semantic_search",)) in unlocks
    logs = [e["message"] for e in events if e["type"] == "log"]
    assert f"Embedded {chunks:,} chunks (0 texts from the cache). Semantic search is ready." in logs


def test_a_second_index_of_the_same_code_embeds_nothing_and_pays_nothing(
    voyage: FakeVoyage,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)
    sent = len(voyage.calls)

    later = runtime.run(_next_job(runtime.system_database, org_id, repo_id))
    _run(org_id, repo_id, later)

    _, job = runtime.run(_state(runtime.system_database, repo_id, later))
    details = job.stages["embeddings"]["details"]
    chunks = job.stages["chunks"]["details"]["chunks"]
    assert (job.stages["embeddings"]["status"], details["embedded"]) == ("done", 0)
    assert details["cached"] == chunks  # every text from the cache
    assert len(voyage.calls) == sent  # nothing sent
    with_vector, without, usage, _ = runtime.run(_embedding_state(runtime, org_id, repo_id, later))
    assert (with_vector, without, usage) == (chunks, 0, [])  # nothing billed


def test_voyage_refusing_skips_semantic_search_and_the_index_still_finishes(
    voyage: FakeVoyage,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    org_id, repo_id, job_id = ids
    voyage.refusal = VoyageError(401, "invalid API key")
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    stage = job.stages["embeddings"]
    assert (stage["status"], stage["details"]["reason"]) == ("skipped", "voyage_error")
    events = runtime.run(_events(runtime, job_stream(job_id)))
    unlocked = [f for e in events if e["type"] == "unlock" for f in e["unlocks"]]
    assert "semantic_search" not in unlocked
    assert "keyword_search" in unlocked
    warned = [e["message"] for e in events if e["type"] == "log" and e["level"] == "warn"]
    assert "Semantic search skipped: Voyage refused (Voyage 401: invalid API key)." in warned


async def _rechunk(database: Database, job_id: uuid.UUID) -> int:
    """The job's snapshot chunked again, as a later run would: its live chunks copied under new
    ids into a new generation, made live (the old rows go). Returns the new generation."""
    async with database.session() as db:
        job = await db.get(IndexJob, job_id)
        assert job is not None
        snapshot = await db.get(Snapshot, job.snapshot_id)
        assert snapshot is not None
        later = await new_generation(db)
        rows = list(
            await db.scalars(
                select(CodeChunk).where(
                    CodeChunk.snapshot_id == snapshot.id,
                    CodeChunk.generation == snapshot.chunk_generation,
                )
            )
        )
        columns = [c.key for c in sa_inspect(CodeChunk).column_attrs]
        for row in rows:
            copy = {key: getattr(row, key) for key in columns}
            copy.update(id=uuid7(), generation=later, embedding=None)
            db.add(CodeChunk(**copy))
        await db.flush()
        await make_live(db, snapshot, later, len(rows))
        return later


def test_chunks_redone_mid_embedding_are_left_to_the_index_that_redid_them(
    voyage: FakeVoyage,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-chunk makes a new generation live while the first group is with Voyage: the group's
    vectors go nowhere (their rows are gone), the stage finds nothing left of its generation and
    finishes; the next index embeds the new rows, from the cache where it can."""
    org_id, repo_id, job_id = ids
    monkeypatch.setattr(embed_task, "GROUP", 2)
    generations: list[int] = []

    async def rechunk() -> None:
        generations.append(await _rechunk(runtime.system_database, job_id))

    voyage.before.append(rechunk)
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.SUCCEEDED
    stage = job.stages["embeddings"]
    assert (stage["status"], stage["details"]["superseded"]) == ("done", True)
    assert len(voyage.calls) == 1  # one group sent, then nothing left of its generation
    chunks = job.stages["chunks"]["details"]["chunks"]
    with_vector, without, _, _ = runtime.run(_embedding_state(runtime, org_id, repo_id, job_id))
    assert (with_vector, without) == (0, chunks)  # the live rows: the new generation's
    events = runtime.run(_events(runtime, job_stream(job_id)))
    logs = [e["message"] for e in events if e["type"] == "log"]
    assert (
        "The chunks were redone while they were embedded: the index that redid them"
        " embeds the new ones." in logs
    )

    later = runtime.run(_next_job(runtime.system_database, org_id, repo_id))
    _run(org_id, repo_id, later)
    _, job = runtime.run(_state(runtime.system_database, repo_id, later))
    details = job.stages["embeddings"]["details"]
    assert "superseded" not in details
    assert details["cached"] == 2  # the group sent before the re-chunk
    with_vector, without, _, _ = runtime.run(_embedding_state(runtime, org_id, repo_id, later))
    assert (with_vector, without) == (chunks, 0)


def test_summaries_follow_embeddings(
    voyage: FakeVoyage,
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.SUCCEEDED
    assert job.stages["embeddings"]["status"] == job.stages["summaries"]["status"] == "done"
    events = runtime.run(_events(runtime, job_stream(job_id)))
    started = [e["stage"] for e in events if e["type"] == "stage_started"]
    assert started.index("embeddings") < started.index("summaries") < started.index("finalize")


# ---------------------------------------------------------------- the code graph


async def _graph(database: Database, snapshot_id: uuid.UUID) -> set[tuple[str, str, str]]:
    async with database.session() as db:
        src, dst = CodeNode.__table__.alias("src"), CodeNode.__table__.alias("dst")
        rows = await db.execute(
            select(src.c.symbol_key, dst.c.symbol_key, CodeEdge.type)
            .join(src, src.c.id == CodeEdge.src_id)
            .join(dst, dst.c.id == CodeEdge.dst_id)
            .where(CodeEdge.snapshot_id == snapshot_id)
        )
        return {(a, b, kind.value) for a, b, kind in rows.tuples()}


def test_the_graph_stage_writes_the_snapshots_code_graph(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    """Every language's resolver: Python's calls, TypeScript's import and call."""
    github.get(COMMITS_URL).mock(return_value=httpx.Response(200, json={"sha": bare_repo.third}))
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)

    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.snapshot_id is not None
    assert runtime.run(_graph(runtime.system_database, job.snapshot_id)) == {
        ("py src/app.py/main().", "py src/app.py/Greeter#", "calls"),
        ("py src/app.py/main().", "py src/app.py/Greeter#greet().", "calls"),
        ("ts web/main.ts/", "ts web/greet.ts/greet().", "imports"),
        ("ts web/main.ts/main().", "ts web/greet.ts/greet().", "calls"),
    }
    details = job.stages["graph"]["details"]["resolvers"]
    assert {name: d["files"] for name, d in details.items()} == {"ecma": 2, "python": 1}


def test_a_chain_sent_without_the_graph_stage_gets_it_from_finalize(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    """A job whose chain was sent before the deploy that added the graph stage."""
    org_id, repo_id, job_id = ids
    kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    old = [
        stage
        for stage in orchestrate.STAGES
        if stage not in (graph_task.build_graph, links_task.build_links)
    ]
    chain(*(stage.si(**kwargs) for stage in old)).apply().get()

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    assert job.stages["graph"]["status"] == job.stages["links"]["status"] == "done"
    assert job.snapshot_id is not None
    assert len(runtime.run(_graph(runtime.system_database, job.snapshot_id))) == 2


def test_a_chain_sent_without_the_links_stage_gets_it_from_finalize(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
) -> None:
    """A job whose chain was sent before the deploy that added the links stage."""
    org_id, repo_id, job_id = ids
    kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
    old = [stage for stage in orchestrate.STAGES if stage is not links_task.build_links]
    chain(*(stage.si(**kwargs) for stage in old)).apply().get()

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    assert job.stages["links"]["status"] == "done"


# ---------------------------------------------------------------- the links


async def _artifact(
    runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID, sha: str
) -> GraphSnapshot:
    storage = await runtime.storage()
    data = await storage.get_bytes(
        Bucket.ARTIFACTS, artifact_key(org_id, repo_id, sha, ARTIFACT_NAME), max_bytes=1 << 26
    )
    assert data is not None
    return GraphSnapshot.loads(data)


def test_the_links_stage_joins_a_page_its_route_and_the_table(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    """The page's fetch targets the route, the route is handled by its function, which reads
    the model's table; and the graph artifact the API reads has both layers, under the links'
    version."""
    github.get(COMMITS_URL).mock(return_value=httpx.Response(200, json={"sha": bare_repo.fourth}))
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.last_indexed_sha) == (JobStatus.SUCCEEDED, bare_repo.fourth)
    assert job.snapshot_id is not None
    call = "http_client_call GET /items stack/page.ts:2"
    assert {
        edge
        for edge in runtime.run(_graph(runtime.system_database, job.snapshot_id))
        if edge[2] in ("issues_request", "targets", "handled_by", "reads", "writes")
    } == {
        ("ts stack/page.ts/loadItems().", call, "issues_request"),
        (call, "api_endpoint GET /items", "targets"),
        ("api_endpoint GET /items", "py stack/api.py/read_items().", "handled_by"),
        ("py stack/api.py/read_items().", "table item", "reads"),
    }
    details = job.stages["links"]["details"]
    assert details["linkers"]["routes"]["endpoints"] == 1
    assert details["linkers"]["urls"]["linked"] == 1
    assert details["introspected"] is False
    assert "Tracing is ready" in str(runtime.run(_events(runtime, job_stream(job_id))))
    artifact = runtime.run(_artifact(runtime, org_id, repo_id, bare_repo.fourth))
    assert artifact.version == details["version"] != job.stages["graph"]["details"]["version"]
    assert artifact.node("table item") is not None


def test_storage_failing_as_the_graph_is_written_retries_the_stage_instead_of_skipping_it(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a resolver's failure costs the graph: storage (or the database) failing for a moment
    is retried, and the graph is built."""
    org_id, repo_id, job_id = ids
    storage = runtime.run(runtime.storage())
    real = storage.put_bytes
    stops = [RetryableError("storage went away")]

    async def put_bytes(bucket: Bucket, key: str, data: bytes, **kwargs: Any) -> Any:
        if key.endswith(ARTIFACT_NAME) and stops:
            raise stops.pop()
        return await real(bucket, key, data, **kwargs)

    monkeypatch.setattr(storage, "put_bytes", put_bytes)
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert not stops  # it failed once
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    assert job.stages["graph"]["status"] == "done"
    assert job.snapshot_id is not None
    assert runtime.run(_graph(runtime.system_database, job.snapshot_id))


def test_storage_failing_as_the_links_are_written_retries_the_stage_instead_of_skipping_it(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a linker's failure costs the links: storage failing for a moment as the links'
    artifact is written (the graph stage's is the first) is retried, and the links are built."""
    org_id, repo_id, job_id = ids
    storage = runtime.run(runtime.storage())
    real = storage.put_bytes
    writes: list[str] = []
    stops = [RetryableError("storage went away")]

    async def put_bytes(bucket: Bucket, key: str, data: bytes, **kwargs: Any) -> Any:
        if key.endswith(ARTIFACT_NAME):
            writes.append(key)
            if len(writes) == 2 and stops:
                raise stops.pop()
        return await real(bucket, key, data, **kwargs)

    monkeypatch.setattr(storage, "put_bytes", put_bytes)
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert not stops  # it failed once
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    assert job.stages["graph"]["status"] == job.stages["links"]["status"] == "done"


def test_a_linker_that_fails_skips_the_links_and_the_index_still_finishes(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, repo_id, job_id = ids

    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("a linker bug")

    monkeypatch.setattr(links_task, "resolve_links", broken)
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    assert job.stages["links"]["status"] == "skipped"
    assert job.stages["links"]["details"]["reason"] == "links_failed"
    assert job.stages["graph"]["status"] == "done"


def test_a_resolver_that_fails_skips_the_graph_and_the_index_still_finishes(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, repo_id, job_id = ids

    def broken(*_: Any) -> None:
        raise RuntimeError("a resolver bug")

    monkeypatch.setattr(graph_stage.RESOLVERS["python"], "resolve", broken)
    _run(org_id, repo_id, job_id)

    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert (job.status, repo.status) == (JobStatus.SUCCEEDED, RepoStatus.READY)
    assert job.stages["graph"]["status"] == "skipped"
    assert job.stages["graph"]["details"]["reason"] == "graph_failed"
    assert job.snapshot_id is not None
    assert runtime.run(_graph(runtime.system_database, job.snapshot_id)) == set()


# ---------------------------------------------------------------- pushes (5.1)


async def _pin(database: Database, job_id: uuid.UUID, ref: str | None, sha: str | None) -> None:
    async with database.session() as db:
        job = await db.get(IndexJob, job_id)
        assert job is not None
        job.ref, job.target_sha = ref, sha


def test_a_pushed_commit_is_indexed_as_it_was_pushed(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    """The push named its commit: that one is indexed, not the branch's head when the run got
    to it (GitHub isn't asked)."""
    org_id, repo_id, job_id = ids
    runtime.run(_pin(runtime.system_database, job_id, "main", bare_repo.first))
    _run(org_id, repo_id, job_id)
    repo, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.SUCCEEDED
    assert repo.last_indexed_sha == bare_repo.first  # the head is second; first was pushed
    assert not github.routes[0].called


def test_a_tracked_branchs_run_leaves_the_repositorys_index_as_it_was(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    _run(org_id, repo_id, job_id)  # main, at its head: second
    branch = runtime.run(_queued(runtime.system_database, org_id, repo_id))
    runtime.run(_pin(runtime.system_database, branch, "web", bare_repo.third))
    _run(org_id, repo_id, branch)
    repo, job = runtime.run(_state(runtime.system_database, repo_id, branch))
    assert job.status is JobStatus.SUCCEEDED
    assert repo.last_indexed_sha == bare_repo.second  # still main's
    main = runtime.run(_chunks_at(runtime.system_database, repo_id, bare_repo.second))
    assert main > 0  # main's chunks weren't pruned by the branch's run


async def _queued(database: Database, org_id: uuid.UUID, repo_id: uuid.UUID) -> uuid.UUID:
    async with database.session() as db:
        job = IndexJob(org_id=org_id, repo_id=repo_id, kind=JobKind.INCREMENTAL)
        db.add(job)
        await db.flush()
        return job.id


async def _chunks_at(database: Database, repo_id: uuid.UUID, sha: str) -> int:
    async with database.session() as db:
        snapshot = await db.scalar(
            select(Snapshot).where(Snapshot.repo_id == repo_id, Snapshot.commit_sha == sha)
        )
        assert snapshot is not None
        count = await db.scalar(
            select(func.count()).select_from(CodeChunk).where(CodeChunk.snapshot_id == snapshot.id)
        )
        return int(count or 0)


async def _settle(database: Database, job_id: uuid.UUID) -> None:
    async with database.session() as db:
        job = await db.get(IndexJob, job_id)
        assert job is not None
        job.status = JobStatus.CANCELLED


def test_a_push_timer_waits_for_a_run_then_starts_the_branchs_once(
    runtime: Runtime, ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID], bare_repo: Any
) -> None:
    org_id, repo_id, queued = ids
    redis = runtime.redis

    async def noted(token: str, after: str) -> None:
        await pushes.note(
            redis, repo_id, "main", after=after, before=None, forced=False, token=token
        )

    def timer(token: str, branch: str = "main") -> tuple[Outcome, IndexJob | None]:
        return runtime.run(
            begin(
                start_push_index, redis, org_id=org_id, repo_id=repo_id, branch=branch, token=token
            )
        )

    runtime.run(noted("t1", bare_repo.first))
    runtime.run(noted("t2", bare_repo.second))
    assert timer("t1") == (Outcome.SUPERSEDED, None)  # a later push's timer has it
    assert timer("t2") == (Outcome.WAITING, None)  # the repository has a run queued
    runtime.run(_settle(runtime.system_database, queued))
    outcome, job = timer("t2")
    assert outcome is Outcome.STARTED
    assert job is not None
    assert (job.kind, job.ref, job.target_sha) == (JobKind.FULL, "main", bare_repo.second)
    assert timer("t2") == (Outcome.SUPERSEDED, None)  # it was indexed: the pending push went
    # A branch the repository doesn't track is let go.
    runtime.run(
        pushes.note(redis, repo_id, "other", after="e" * 40, before=None, forced=False, token="t3")
    )
    assert timer("t3", "other") == (Outcome.UNTRACKED, None)
    assert runtime.run(pushes.due(redis, repo_id, "other", "t3")) is None


# ---------------------------------------------------------------- incremental runs (5.2)


async def _paths_and_vectors(
    database: Database, repo_id: uuid.UUID, sha: str
) -> tuple[Snapshot, dict[str, int], set[str]]:
    """The snapshot of ``sha``, its live code chunks by path (each counted), and the paths
    whose chunks all have a vector."""
    async with database.session() as db:
        snapshot = await db.scalar(
            select(Snapshot).where(Snapshot.repo_id == repo_id, Snapshot.commit_sha == sha)
        )
        assert snapshot is not None
        rows = list(
            await db.execute(
                select(CodeChunk.path, CodeChunk.embedding.is_not(None)).where(
                    CodeChunk.snapshot_id == snapshot.id,
                    CodeChunk.generation == snapshot.chunk_generation,
                    CodeChunk.view == "code",
                )
            )
        )
    counted: dict[str, int] = {}
    vectorless: set[str] = set()
    for path, has in rows:
        counted[path] = counted.get(path, 0) + 1
        if not has:
            vectorless.add(path)
    return snapshot, counted, set(counted) - vectorless


async def _symbol_keys(database: Database, snapshot_id: uuid.UUID) -> set[str]:
    async with database.session() as db:
        found = await db.scalars(
            select(CodeNode.symbol_key).where(
                CodeNode.snapshot_id == snapshot_id, CodeNode.kind.in_(sorted(SYMBOL_KINDS))
            )
        )
        return set(found)


def _incremental(runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID, sha: str) -> IndexJob:
    job_id = runtime.run(_queued(runtime.system_database, org_id, repo_id))
    runtime.run(_pin(runtime.system_database, job_id, None, sha))
    _run(org_id, repo_id, job_id)
    _, job = runtime.run(_state(runtime.system_database, repo_id, job_id))
    assert job.status is JobStatus.SUCCEEDED
    return job


def test_an_incremental_run_reads_only_what_changed_and_a_move_costs_nothing(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
    voyage: FakeVoyage,
) -> None:
    org_id, repo_id, job_id = ids
    database = runtime.system_database
    runtime.run(_pin(database, job_id, None, bare_repo.third))
    _run(org_id, repo_id, job_id)  # in full: the third (the second, and web/)
    third, third_chunks, _ = runtime.run(_paths_and_vectors(database, repo_id, bare_repo.third))
    third_keys = runtime.run(_symbol_keys(database, third.id))
    embedded = sum(n for n, _ in voyage.calls)
    assert embedded > 0

    # A pure move: client/greet.ts is web/greet.ts, moved. Only it is read again, and its
    # chunks take their vectors from the old path's: nothing is embedded.
    moved = _incremental(runtime, org_id, repo_id, bare_repo.fifth)
    assert moved.kind is JobKind.INCREMENTAL
    assert moved.stages["parse"]["details"]["files"] == 1
    assert moved.stages["chunks"]["details"]["files"] == 1
    assert moved.stages["chunks"]["details"]["moved_vectors"] > 0
    assert sum(n for n, _ in voyage.calls) == embedded  # nothing more was embedded
    fifth, fifth_chunks, with_vectors = runtime.run(
        _paths_and_vectors(database, repo_id, bare_repo.fifth)
    )
    assert "web/greet.ts" not in fifth_chunks
    assert fifth_chunks["client/greet.ts"] == third_chunks["web/greet.ts"]
    assert set(fifth_chunks) == with_vectors  # every chunk has its vector
    kept = {k for k in third_keys if "web/greet.ts" not in k}
    fifth_keys = runtime.run(_symbol_keys(database, fifth.id))
    assert kept <= fifth_keys  # the kept files' definitions, carried
    assert any("client/greet.ts" in k for k in fifth_keys)  # the moved file's, read again
    assert moved.stages["symbols"]["details"]["carried"] == len(
        {k for k in fifth_keys if "client/greet.ts" not in k}
    )

    # One file changed, one deleted: only the changed one is read again and embedded.
    changed = _incremental(runtime, org_id, repo_id, bare_repo.sixth)
    assert changed.stages["parse"]["details"]["files"] == 1
    sixth, sixth_chunks, with_vectors = runtime.run(
        _paths_and_vectors(database, repo_id, bare_repo.sixth)
    )
    assert "README.md" not in sixth_chunks  # deleted: none of its facts are carried
    assert set(sixth_chunks) == with_vectors
    sixth_keys = runtime.run(_symbol_keys(database, sixth.id))
    assert any(k.endswith("quiet().") or "quiet" in k for k in sixth_keys)  # the new function
    assert sum(n for n, _ in voyage.calls) - embedded <= sixth_chunks["web/main.ts"]
    repo, _ = runtime.run(_state(database, repo_id, changed.id))
    assert repo.last_indexed_sha == bare_repo.sixth


def test_an_incremental_run_summarizes_only_the_files_that_changed(
    claude: FakeAnthropic,
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    database = runtime.system_database
    runtime.run(_pin(database, job_id, None, bare_repo.third))
    _run(org_id, repo_id, job_id)
    [first] = claude.bodies(BATCHES)
    full = len(first["requests"])
    assert full >= 3  # src/app.py, web/greet.ts and web/main.ts
    rollups_before = len(claude.bodies("/v1/messages"))

    # A move: only the moved file is summarized again; the others keep theirs.
    moved = _incremental(runtime, org_id, repo_id, bare_repo.fifth)
    sent = claude.bodies(BATCHES)[1]["requests"]
    assert len(sent) == 1
    assert "client/greet.ts" in json.dumps(sent)
    details = moved.stages["summaries"]["details"]
    assert details["reused"] == full - 1
    artifact, _, _ = runtime.run(_summaries(runtime, org_id, repo_id, moved.id, bare_repo.fifth))
    paths = {file["path"] for file in artifact["files"]}
    assert {"src/app.py", "web/main.ts", "client/greet.ts"} <= paths
    assert "web/greet.ts" not in paths
    # Directories nothing under which changed keep their rollups: src/ isn't asked about.
    asked = [
        b["messages"][0]["content"].split("\n")[0]
        for b in claude.bodies("/v1/messages")[rollups_before:]
        if b["messages"][0]["content"].startswith("<directory>")
    ]
    # web/ lost greet.ts and client/ is new (and so the root); src/ is as it was.
    assert sorted(asked) == [
        "<directory>(repository root)</directory>",
        "<directory>client</directory>",
        "<directory>web</directory>",
    ]

    # One file changed: it alone is sent.
    _incremental(runtime, org_id, repo_id, bare_repo.sixth)
    sent = claude.bodies(BATCHES)[2]["requests"]
    assert len(sent) == 1
    assert "web/main.ts" in json.dumps(sent)


# ---------------------------------------------------------------- resolution again (5.3)


def test_an_incremental_rename_moves_its_callers_edges_to_the_new_name(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    database = runtime.system_database
    runtime.run(_pin(database, job_id, None, bare_repo.sixth))
    _run(org_id, repo_id, job_id)
    _, before = runtime.run(_state(database, repo_id, job_id))
    assert before.snapshot_id is not None
    calls = {e for e in runtime.run(_graph(database, before.snapshot_id)) if e[2] == "calls"}
    assert ("ts web/main.ts/main().", "ts client/greet.ts/greet().", "calls") in calls

    renamed = _incremental(runtime, org_id, repo_id, bare_repo.seventh)
    assert renamed.snapshot_id is not None
    graph = runtime.run(_graph(database, renamed.snapshot_id))
    assert ("ts web/main.ts/main().", "ts client/greet.ts/hello().", "calls") in graph
    old = "ts client/greet.ts/greet()."
    assert not any(old in (a, b) for a, b, _ in graph)  # the old name's edges went, not left stale


def test_an_incremental_run_that_removes_an_endpoint_leaves_a_gap_in_its_trace(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    database = runtime.system_database
    runtime.run(_pin(database, job_id, None, bare_repo.fourth))
    _run(org_id, repo_id, job_id)

    removed = _incremental(runtime, org_id, repo_id, bare_repo.eighth)
    assert removed.snapshot_id is not None
    call = "http_client_call GET /items stack/page.ts:2"
    links = {
        edge
        for edge in runtime.run(_graph(database, removed.snapshot_id))
        if edge[2] in ("issues_request", "targets", "handled_by")
    }
    assert links == {("ts stack/page.ts/loadItems().", call, "issues_request")}  # nothing to target
    graph = runtime.run(_artifact(runtime, org_id, repo_id, bare_repo.eighth))
    assert graph.node("api_endpoint GET /items") is None
    traced = trace_feature(graph, "ts stack/page.ts/loadItems().", direction="down")
    best = traced.paths[0]
    assert not best.reaches_goal
    assert any(gap.kind == "gap" for gap in best.breaks)  # the call reaches no endpoint


def test_the_apps_routes_are_read_again_only_when_python_changed() -> None:
    assert not links_task.introspect_again({"web/main.ts", "README.md"})
    assert links_task.introspect_again({"web/main.ts", "stack/api.py"})
    assert links_task.introspect_again({"app/types.pyi"})
    assert links_task.introspect_again({"backend/pyproject.toml"})  # its dependencies
    assert links_task.introspect_again({"backend/.env.example"})  # its configuration
    routes = RuntimeRoutes(
        [Route("GET", "/items", "py a.py/read().", "runtime", tags=("items",), entry="a:app")],
        limitation=None,
        notes=["one app"],
        detail="ok",
    )
    assert links_task.routes_from(links_task.routes_json(routes)) == routes
    # Another version's, or one that can't be read, is read again from the apps.
    assert links_task.routes_from(b'{"version": 0, "routes": []}') is None
    assert links_task.routes_from(b'{"version": 1, "routes": [{"nope": 1}]}') is None


# ---------------------------------------------------------------- overlays (5.4)


async def _read(
    database: Database, repo_id: uuid.UUID, ref: str | None
) -> tuple[Snapshot, set[str], int]:
    """What ``ref`` reads (as the API resolves it): its snapshot, its definitions' keys, and its
    live chunks."""
    async with database.session() as db:
        repo = await db.get(Repo, repo_id)
        assert repo is not None
        snapshot = await resolve_snapshot(db, repo, ref)
        names = set(
            await db.scalars(select(CodeNode.symbol_key).where(CodeNode.snapshot_id == snapshot.id))
        )
        chunks = await db.scalar(
            select(func.count())
            .select_from(CodeChunk)
            .where(
                CodeChunk.snapshot_id == snapshot.id,
                CodeChunk.generation == snapshot.chunk_generation,
            )
        )
        return snapshot, names, int(chunks or 0)


def test_a_branch_overlay_sees_its_new_function_and_stays_searchable_when_main_moves_on(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    database = runtime.system_database
    _run(org_id, repo_id, job_id)  # main, at the second
    branch = runtime.run(_queued(database, org_id, repo_id))
    runtime.run(_pin(database, branch, "web", bare_repo.third))
    _run(org_id, repo_id, branch)
    main = runtime.run(_queued(database, org_id, repo_id))  # main indexed again: it prunes
    runtime.run(_pin(database, main, None, bare_repo.second))
    _run(org_id, repo_id, main)

    web, web_names, web_chunks = runtime.run(_read(database, repo_id, "web"))
    head, main_names, _ = runtime.run(_read(database, repo_id, None))
    assert web.commit_sha == bare_repo.third
    added = "ts web/greet.ts/greet()."
    assert added in web_names  # the branch's new function
    assert added not in main_names  # main doesn't have it
    assert web_chunks > 0  # still searchable after main's run pruned
    assert web.overlay_of == head.id  # built on main's index


def test_a_closed_pull_requests_overlay_is_dropped(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    org_id, repo_id, job_id = ids
    database = runtime.system_database
    _run(org_id, repo_id, job_id)
    pull = runtime.run(_queued(database, org_id, repo_id))
    runtime.run(_pin(database, pull, "pull/3", bare_repo.third))
    _run(org_id, repo_id, pull)
    assert runtime.run(_read(database, repo_id, "pull/3"))[0].commit_sha == bare_repo.third
    dropped = maintenance.drop_ref.apply(
        kwargs={"org_id": str(org_id), "repo_id": str(repo_id), "ref": "pull/3"}
    ).get()
    assert dropped == 1
    with pytest.raises(NotFoundError):
        runtime.run(_read(database, repo_id, "pull/3"))
    assert runtime.run(_read(database, repo_id, None))[0].commit_sha == bare_repo.second


async def _track(database: Database, repo_id: uuid.UUID, branch: str) -> None:
    async with database.session() as db:
        repo = await db.get(Repo, repo_id)
        assert repo is not None
        repo.settings = {**(repo.settings or {}), "tracked_branches": [branch]}


def test_a_push_of_a_commit_already_indexed_runs_nothing_and_its_branch_reads_it(
    runtime: Runtime,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    github: respx.MockRouter,
    bare_repo: Any,
) -> None:
    """A branch made from the default branch (its first push names main's commit): no run, its
    ref points at the snapshot there is, and it reads it by its name."""
    org_id, repo_id, job_id = ids
    database = runtime.system_database
    _run(org_id, repo_id, job_id)  # main, at the second
    runtime.run(_track(database, repo_id, "release"))
    redis = runtime.redis
    runtime.run(
        pushes.note(
            redis, repo_id, "release", after=bare_repo.second, before=None, forced=False, token="r"
        )
    )
    outcome, job = runtime.run(
        begin(start_push_index, redis, org_id=org_id, repo_id=repo_id, branch="release", token="r")
    )
    assert (outcome, job) == (Outcome.INDEXED, None)
    release, _, chunks = runtime.run(_read(database, repo_id, "release"))
    assert release.commit_sha == bare_repo.second
    assert chunks > 0
