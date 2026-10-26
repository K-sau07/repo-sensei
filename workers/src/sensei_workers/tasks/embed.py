"""The embeddings stage (2.7): a vector for every chunk of the snapshot, then the HNSW index of
the repository's partition of ``chunks``. It unlocks semantic search and ask.

It follows chunks. Chunks without a vector are taken :data:`GROUP` at a time, in id order, each
group in its own transaction: their vectors come from the org's embedding cache (by content
hash, model and dimensions) where it has them, and from Voyage otherwise (cached as they come,
usage in the ledger); then they are written to the chunks. A task that stops part way picks up
the chunks still without a vector: the groups it finished are cached and saved; only the group in
flight (at most :data:`GROUP` texts) may be sent again.
A second index of the same code finds every vector in the cache, for nothing.

The stage embeds the snapshot's live generation of chunks as it starts (the chunks stage before
it made it live). If a re-chunk makes another generation live part way, that run deletes the rows
being embedded, vectors and all; this stage finds nothing left of its generation and finishes,
and the new rows are embedded by the stage that follows the re-chunk.

Once every chunk has its vector, the partition's HNSW index is built if it has none yet (one
build after the first bulk load beats inserting into the graph row by row; later loads go into
the index as they come). The build runs as the system role (only the tables' owner may create
an index: a ``SECURITY DEFINER`` function does it).

Embeddings are optional: with no Voyage key the stage is passed over (keyword search still
works). Voyage out of reach or rate limited past its own retries: tried again later, then
skipped. Voyage refusing (a bad key, a request it won't take): skipped with the reason. Either
way the index finishes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, Repo, Snapshot, Stage
from sensei_engine.embed import store
from sensei_engine.embed.cache import embed_documents
from sensei_engine.embed.provider import usage_ledger
from sensei_engine.embed.voyage import (
    VoyageError,
    VoyageNotConfigured,
    VoyageRateLimited,
    VoyageUnreachable,
)
from sensei_engine.search.bm25 import chunk_partition
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError
from sensei_workers.jobstate import JobScope, job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.statistics import analyze
from sensei_workers.tasks.ingest import IngestTask

log = get_logger(__name__)

AFTER = Stage.CHUNKS
STAGE = Stage.EMBEDDINGS
REF_TYPE = "index_job"  # the usage belongs to the job
GROUP = 1_000  # chunks per transaction (one cache lookup, at most one Voyage request group)
# Attempts that may end with Voyage out of reach before the stage gives up (skipped).
MAX_UNAVAILABLE = 6
UNAVAILABLE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class Target:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    job_id: uuid.UUID
    partition: str | None  # the repository's partition of chunks (None: no chunks)
    generation: int | None  # the snapshot's live chunks as the stage starts (None: no chunks)


@dataclass(slots=True)
class Tally:
    chunks: int = 0  # given a vector by this attempt
    cached: int = 0  # of the texts, found in the cache
    embedded: int = 0  # of the texts, sent to Voyage


def _now() -> datetime:
    return datetime.now(UTC)


async def _begin(task: IngestTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or resume) the job's embeddings stage; None if there is nothing to do."""
    async with job_scope(task, org_id, job_id) as scope:
        db, job = scope.db, scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None
        stages = job.stages or {}
        ready = job.stage is AFTER and stages.get(AFTER.value, {}).get("status") == "done"
        resuming = job.stage is STAGE and stages.get(STAGE.value, {}).get("status") == "running"
        if not ready and not resuming:
            order = list(Stage)
            if job.stage is None or order.index(job.stage) <= order.index(AFTER):
                raise NotYetError(f"the {AFTER.value} stage hasn't finished")
            return None  # past this stage already (a duplicate delivery)
        repo = await db.get(Repo, job.repo_id)
        machine = scope.machine
        if repo is None or repo.deleted_at is not None:
            machine.cancel()
            return None
        snapshot = await db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
        if snapshot is None:
            raise NotYetError("the job has no snapshot")
        configured = get_runtime().embeddings is not None
        if ready:
            if not configured:
                machine.log("Semantic search is off: this server has no Voyage API key.")
                return None  # the next stage passes this one over
            machine.advance(STAGE)
        elif not configured:
            _skip(scope, "not_configured", "this server no longer has a Voyage API key")
            return None
        partition = await chunk_partition(db, repo.id)
        generation = snapshot.chunk_generation
        return Target(repo.org_id, repo.id, snapshot.id, job_id, partition, generation)


def _skip(scope: JobScope, reason: str, why: str) -> None:
    scope.machine.skip(STAGE, reason=reason)
    scope.machine.log(f"Semantic search skipped: {why}.", level="warn")


async def _give_up(task: IngestTask, target: Target, reason: str, why: str) -> None:
    async with job_scope(task, target.org_id, target.job_id) as scope:
        scope.require(STAGE)
        _skip(scope, reason, why)


async def _unavailable(task: IngestTask, target: Target, exc: VoyageError) -> float | None:
    """Voyage out of reach or rate limited past its retries: seconds until the next attempt, or
    None once :data:`MAX_UNAVAILABLE` attempts met it (the stage is skipped)."""
    async with job_scope(task, target.org_id, target.job_id) as scope:
        job = scope.require(STAGE)
        details = (job.stages or {}).get(STAGE.value, {}).get("details", {})
        count = int(details.get("unavailable", 0)) + 1
        if count >= MAX_UNAVAILABLE:
            _skip(scope, "voyage_unavailable", f"Voyage was unreachable ({exc})")
            return None
        scope.machine.note(STAGE, unavailable=count)
        scope.machine.keep_alive(STAGE, _now() + timedelta(seconds=UNAVAILABLE_SECONDS * 2))
    return UNAVAILABLE_SECONDS


def _transient(exc: VoyageError) -> bool:
    if isinstance(exc, VoyageNotConfigured):
        return False
    return isinstance(exc, VoyageUnreachable | VoyageRateLimited) or (
        exc.status == 429 or exc.status >= 500
    )


async def _total(task: IngestTask, target: Target) -> tuple[int, int]:
    """How many of the snapshot's chunks have a vector, and how many there are."""
    async with task.org_session(target.org_id) as db:
        missing = await store.count_pending(db, target.snapshot_id)
        snapshot = await db.get(Snapshot, target.snapshot_id)
        total = (snapshot.chunk_count or 0) if snapshot is not None else 0
    total = max(total, missing)
    return total - missing, total


async def _embed_group(
    task: IngestTask, target: Target, after: uuid.UUID | None, tally: Tally
) -> uuid.UUID | None:
    """Give the next group of chunks without a vector theirs (one transaction); the last
    chunk's id, or None when none is left."""
    runtime = get_runtime()
    provider = runtime.embeddings
    if provider is None:
        raise VoyageNotConfigured
    async with task.org_session(target.org_id) as db:
        chunks = await store.pending(
            db, target.snapshot_id, generation=target.generation, after=after, limit=GROUP
        )
        if not chunks:
            return None
        sink = usage_ledger(
            db, runtime.costs, org_id=target.org_id, ref_type=REF_TYPE, ref_id=str(target.job_id)
        )
        found = await embed_documents(
            db,
            provider,
            [(chunk.content_hash, chunk.text) for chunk in chunks],
            org_id=target.org_id,
            on_usage=sink,
        )
        await store.save(db, target.repo_id, {c.id: found.vectors[c.content_hash] for c in chunks})
    tally.chunks += len(chunks)
    tally.cached += found.hits
    tally.embedded += found.embedded
    return chunks[-1].id


async def _progress(task: IngestTask, target: Target, done: int, total: int) -> None:
    async with job_scope(task, target.org_id, target.job_id) as scope:
        scope.require(STAGE)
        scope.machine.update_progress(STAGE, done, total)


async def _build_index(target: Target) -> bool:
    """The partition's HNSW index, built if it has none (whether it was)."""
    if target.partition is None:
        return False
    async with get_runtime().system_database.session() as db:
        return await store.build_index(db, target.partition)


async def _finish(task: IngestTask, target: Target, tally: Tally, *, built: bool) -> None:
    async with job_scope(task, target.org_id, target.job_id) as scope:
        job = scope.require(STAGE)
        details = (job.stages or {}).get(STAGE.value, {}).get("details", {})
        machine = scope.machine
        # Across attempts: a resumed stage adds to what the stopped one did.
        cached = int(details.get("cached", 0)) + tally.cached
        embedded = int(details.get("embedded", 0)) + tally.embedded
        machine.note(
            STAGE,
            cached=cached,
            embedded=embedded,
            index_built=built or bool(details.get("index_built")),
        )
        total = int((job.stages or {}).get(STAGE.value, {}).get("total") or 0)
        machine.update_progress(STAGE, total, total)
        snapshot = await scope.db.get(Snapshot, target.snapshot_id)
        if snapshot is not None and snapshot.chunk_generation != target.generation:
            machine.note(STAGE, superseded=True)
            machine.log(
                "The chunks were redone while they were embedded: the index that redid them"
                " embeds the new ones."
            )
        else:
            machine.log(
                f"Embedded {total:,} chunks ({cached:,} texts from the cache)."
                " Semantic search is ready."
            )
        machine.complete(STAGE)


async def _checkpoint(task: IngestTask, target: Target, tally: Tally) -> None:
    """What this attempt did so far, kept on the stage (a later attempt adds to it)."""
    async with job_scope(task, target.org_id, target.job_id) as scope:
        job = scope.require(STAGE)
        details = (job.stages or {}).get(STAGE.value, {}).get("details", {})
        scope.machine.note(
            STAGE,
            cached=int(details.get("cached", 0)) + tally.cached,
            embedded=int(details.get("embedded", 0)) + tally.embedded,
        )


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.EMBED_CHUNKS.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def embed_snapshot(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> int | None:
    """Embed the job's snapshot's chunks; returns how many got a vector in this attempt (None:
    the stage was passed over or skipped, or the job moved on)."""
    runtime = get_runtime()
    target = runtime.run(_begin(self, org_id, uuid.UUID(job_id)))
    if target is None:
        return None
    tally = Tally()
    try:
        done, total = runtime.run(_total(self, target))
        runtime.run(_progress(self, target, done, total))
        after: uuid.UUID | None = None
        while True:
            after = runtime.run(_embed_group(self, target, after, tally))
            if after is None:
                break
            runtime.run(_progress(self, target, done + tally.chunks, total))
    except VoyageError as exc:
        if tally.chunks:
            runtime.run(_checkpoint(self, target, tally))
        if not _transient(exc):
            reason = "not_configured" if isinstance(exc, VoyageNotConfigured) else "voyage_error"
            runtime.run(_give_up(self, target, reason, f"Voyage refused ({exc})"))
            return None
        wait = runtime.run(_unavailable(self, target, exc))
        if wait is None:
            return None
        self.wait_again(wait, exc)
    built = runtime.run(_build_index(target))
    runtime.run(analyze("chunks"))
    runtime.run(_finish(self, target, tally, built=built))
    log.info(
        "chunks embedded",
        chunks=tally.chunks,
        cached=tally.cached,
        embedded=tally.embedded,
        index_built=built,
    )
    return tally.chunks
