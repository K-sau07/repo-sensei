"""The chunks stage: the snapshot's files cut into retrieval units and saved as ``chunks``.

``chunk_files`` runs after the symbols stage (a definition's chunks link to its node), on the
same working copy. Files are chunked in a pool of processes (:mod:`sensei_engine.chunk.stage`),
code parsed again there. Chunks are saved as they come, ``BATCH_ROWS`` rows at a time
(:class:`~sensei_engine.chunk.store.ChunkWriter`), so a large repository's never sit in memory
at once.

A run writes the snapshot's chunks under a new generation, each batch committed on its own (no
transaction lasts the stage: VACUUM, DDL and the rest of the database never wait on it). Search
keeps reading the live generation meanwhile; once every file is written, one short transaction
makes the new one live (with ``chunk_count``), deletes the old one's rows and completes the
stage (see :mod:`sensei_engine.chunk.store`). A run that fails or is cancelled deletes what it
wrote and leaves the live generation as it was; so does a reindex of the same commit (its files
keep their ids when discovered again: only the chunks of files that are gone go before this
stage). A run that couldn't delete its rows (its worker killed, the database gone) leaves them
to the next run, which deletes every earlier generation that isn't live as it starts.
Afterwards the repository's partition is analyzed, or vacuumed when rows were deleted
(:mod:`sensei_workers.statistics`: deleted rows count in its BM25 statistics until a VACUUM).
Progress is reported in files, at most once a second, each report in its own transaction (which
also stops the stage if the job was cancelled).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, Repo, RepoFile, Snapshot, Stage
from sensei_engine.chunk.stage import ChunkReport, chunk_files, chunkable
from sensei_engine.chunk.store import (
    ChunkWriter,
    FileChunks,
    SnapshotKey,
    discard_generation,
    make_live,
    new_generation,
    sweep_stale_generations,
)
from sensei_engine.snapshots import ArchiveTooLargeError
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.ingest.cache import repo_lock
from sensei_workers.jobstate import JobGoneError, job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.statistics import analyze_chunks
from sensei_workers.tasks.discover import SnapshotUnavailableError, local_worktree
from sensei_workers.tasks.ingest import IngestTask, fail_job
from sensei_workers.tasks.maintenance import request_vacuum

log = get_logger(__name__)

PROGRESS_EVERY_SECONDS = 1.0
STAGE_ORDER = list(Stage)


Files = dict[str, tuple[uuid.UUID, str | None]]  # path -> (file id, language)


@dataclass(frozen=True, slots=True)
class Target:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str


async def _begin(task: SenseiTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or restart) the job's chunks stage; None if there is nothing to do."""
    async with job_scope(task, org_id, job_id) as scope:
        db, job = scope.db, scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None
        stages = job.stages or {}
        symbols_done = (
            job.stage is Stage.SYMBOLS and stages.get("symbols", {}).get("status") == "done"
        )
        resuming = job.stage is Stage.CHUNKS and stages["chunks"].get("status") == "running"
        if not symbols_done and not resuming:
            if job.stage is None or STAGE_ORDER.index(job.stage) <= STAGE_ORDER.index(
                Stage.SYMBOLS
            ):
                raise NotYetError("the symbols stage hasn't finished")
            return None  # past this stage already (a duplicate delivery)
        repo = await db.get(Repo, job.repo_id)
        if repo is None or repo.deleted_at is not None:
            scope.machine.cancel()
            return None
        snapshot = await db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
        if snapshot is None:
            raise NotYetError("the job has no snapshot yet")
        if symbols_done:
            scope.machine.advance(Stage.CHUNKS)
        return Target(repo.org_id, repo.id, snapshot.id, snapshot.commit_sha)


async def _files(task: SenseiTask, target: Target) -> Files:
    """Path to ``(file id, language)`` for the snapshot's files we chunk, in path order."""
    async with task.org_session(target.org_id) as db:
        rows = await db.execute(
            select(RepoFile.path, RepoFile.id, RepoFile.language, RepoFile.flags)
            .where(RepoFile.snapshot_id == target.snapshot_id)
            .order_by(RepoFile.path)
        )
        return {
            path: (file_id, language)
            for path, file_id, language, flags in rows
            if chunkable(path, language, flags or ())
        }


async def _progress(
    task: SenseiTask, target: Target, job_id: uuid.UUID, done: int, total: int
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        job = scope.require(Stage.CHUNKS)
        # A retry starts over: its count stays at the last attempt's until it gets past it.
        previous = int((job.stages or {}).get("chunks", {}).get("done") or 0)
        scope.machine.update_progress(Stage.CHUNKS, max(done, previous), total)


async def _generation(task: SenseiTask, target: Target) -> tuple[int, int]:
    """A new generation for this run, and how many rows of runs that stopped were deleted."""
    async with task.org_session(target.org_id) as db:
        generation = await new_generation(db)
        swept = await sweep_stale_generations(db, target.repo_id, below=generation)
        return generation, swept


async def _write(
    task: SenseiTask,
    target: Target,
    job_id: uuid.UUID,
    root: Path,
    files: Files,
    *,
    generation: int,
) -> tuple[ChunkReport, int]:
    """Chunk the files under ``generation``, a batch a transaction; returns the report and how
    many chunks were written."""
    report = ChunkReport()
    snapshot = SnapshotKey(target.org_id, target.repo_id, target.snapshot_id)
    file_ids = {path: file_id for path, (file_id, _) in files.items()}
    writer = ChunkWriter(
        lambda: task.org_session(target.org_id), snapshot, file_ids, generation=generation
    )
    pairs = [(path, language) for path, (_, language) in files.items()]
    last = time.monotonic()
    # The pool's results are read on the event loop: nothing else runs on it meanwhile.
    for chunked in chunk_files(root, pairs):
        report.add(chunked)
        flushed = writer.written
        if chunked.chunks:
            await writer.add(FileChunks(chunked.path, chunked.language, chunked.chunks))
        now = time.monotonic()
        if writer.written != flushed or now - last >= PROGRESS_EVERY_SECONDS:
            last = now
            await _progress(task, target, job_id, report.files, len(files))
    return report, await writer.finish()


async def _finish(
    task: SenseiTask,
    target: Target,
    job_id: uuid.UUID,
    report: ChunkReport,
    *,
    generation: int,
    written: int,
    seconds: float,
) -> int:
    """Make the generation live and complete the stage; returns the old rows deleted."""
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(Stage.CHUNKS)
        snapshot = await scope.db.get(Snapshot, target.snapshot_id)
        if snapshot is None:
            raise JobGoneError("the snapshot is gone")
        deleted = await make_live(scope.db, snapshot, generation, written)
        machine = scope.machine
        total = max(report.files, 1)
        machine.update_progress(Stage.CHUNKS, total, total)
        machine.note(Stage.CHUNKS, **report.details(), wall_seconds=round(seconds, 3))
        machine.log(
            f"Cut {report.chunked:,} files into {written:,} chunks. Keyword search is ready."
        )
        if report.failed:
            machine.log(f"{report.failed:,} files couldn't be chunked.", level="warn")
        machine.complete(Stage.CHUNKS)
        return deleted


async def _discard(task: SenseiTask, target: Target, generation: int) -> int:
    async with task.org_session(target.org_id) as db:
        return await discard_generation(db, target.repo_id, target.snapshot_id, generation)


def _discard_quietly(task: SenseiTask, target: Target, generation: int) -> None:
    """Delete what a run that stopped wrote (finalize's prune sweeps it if this fails too)."""
    runtime = get_runtime()
    try:
        if runtime.run(_discard(task, target, generation)):
            request_vacuum(target.repo_id, eager=bool(task.request.is_eager))
    except Exception:
        log.warning("couldn't discard a chunk generation", generation=generation, exc_info=True)


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.CHUNK_FILES.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def chunk_snapshot(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> int | None:
    """Chunk the job's snapshot; returns how many chunks were saved."""
    runtime = get_runtime()
    job = uuid.UUID(job_id)
    target = runtime.run(_begin(self, org_id, job))
    if target is None:
        return None
    files = runtime.run(_files(self, target))
    generation, swept = runtime.run(_generation(self, target))
    if swept:
        request_vacuum(target.repo_id, eager=bool(self.request.is_eager))
    started = time.monotonic()
    try:
        with repo_lock(runtime.settings.ingest.cache_dir, str(target.repo_id)):
            root = local_worktree(target)
            written_by = _write(self, target, job, root, files, generation=generation)
            report, written = runtime.run(written_by)
        seconds = time.monotonic() - started
        deleted = runtime.run(
            _finish(
                self, target, job, report, generation=generation, written=written, seconds=seconds
            )
        )
    except SnapshotUnavailableError:
        _discard_quietly(self, target, generation)
        message = "The snapshot of this commit is missing. Reindex the repository."
        runtime.run(fail_job(self, org_id, job, "snapshot_unavailable", message))
        return None
    except ArchiveTooLargeError:
        _discard_quietly(self, target, generation)
        message = "The snapshot of this commit is larger than the size limit."
        runtime.run(fail_job(self, org_id, job, "snapshot_too_large", message))
        return None
    except BaseException:
        _discard_quietly(self, target, generation)
        raise
    if deleted:
        request_vacuum(target.repo_id, eager=bool(self.request.is_eager))
    else:
        runtime.run(analyze_chunks(target.repo_id))
    log.info(
        "files chunked",
        files=report.files,
        chunked=report.chunked,
        failed=report.failed,
        chunks=written,
    )
    return written
