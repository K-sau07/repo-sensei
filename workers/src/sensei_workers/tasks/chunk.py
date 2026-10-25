"""The chunks stage: the snapshot's files cut into retrieval units and saved as ``chunks``.

``chunk_files`` runs after the symbols stage (a definition's chunks link to its node), on the
same working copy. Files are chunked in a pool of processes (:mod:`sensei_engine.chunk.stage`),
code parsed again there. Chunks are saved as they come, ``BATCH_ROWS`` rows at a time
(:class:`~sensei_engine.chunk.store.ChunkWriter`), so a large repository's never sit in memory
at once.

All of it is one transaction: the snapshot's old chunks are deleted, the new ones written and
``chunk_count`` set together. Keyword search serves a snapshot only once ``chunk_count`` is set;
a stage that runs again (a retry) keeps serving the snapshot's chunks until the new ones commit,
and a run that stops part way leaves them as they were. (A reindex of the same commit discovers
its files again, which takes their chunks along: the snapshot waits for this stage.) Then the
repository's partition is vacuumed and analyzed
(:func:`~sensei_workers.statistics.vacuum_chunks`: the old rows out of its BM25 statistics, the
new ones in the planner's). Progress is reported in files, at most once a second, each report in
its own transaction (which also stops the stage if the job was cancelled).
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
from sensei_engine.chunk.store import ChunkWriter, FileChunks
from sensei_engine.snapshots import ArchiveTooLargeError
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.ingest.cache import repo_lock
from sensei_workers.jobstate import JobGoneError, job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.statistics import vacuum_chunks
from sensei_workers.tasks.discover import SnapshotUnavailableError, local_worktree
from sensei_workers.tasks.ingest import IngestTask, fail_job

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
        scope.require(Stage.CHUNKS)
        scope.machine.update_progress(Stage.CHUNKS, done, total)


async def _rewrite(
    task: SenseiTask, target: Target, job_id: uuid.UUID, root: Path, files: Files
) -> tuple[ChunkReport, int]:
    """Chunk the files and replace the snapshot's chunks with theirs, in one transaction (see
    above); returns the report and how many chunks were written."""
    report = ChunkReport()
    async with task.org_session(target.org_id) as db:
        snapshot = await db.get(Snapshot, target.snapshot_id)
        if snapshot is None:
            raise JobGoneError("the snapshot is gone")
        file_ids = {path: file_id for path, (file_id, _) in files.items()}
        writer = ChunkWriter(db, snapshot, file_ids)
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
        written = await writer.finish()
        snapshot.chunk_count = written
    return report, written


async def _finish(
    task: SenseiTask,
    target: Target,
    job_id: uuid.UUID,
    report: ChunkReport,
    *,
    written: int,
    seconds: float,
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(Stage.CHUNKS)
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
    started = time.monotonic()
    try:
        with repo_lock(runtime.settings.ingest.cache_dir, str(target.repo_id)):
            root = local_worktree(target)
            report, written = runtime.run(_rewrite(self, target, job, root, files))
    except SnapshotUnavailableError:
        message = "The snapshot of this commit is missing. Reindex the repository."
        runtime.run(fail_job(self, org_id, job, "snapshot_unavailable", message))
        return None
    except ArchiveTooLargeError:
        message = "The snapshot of this commit is larger than the size limit."
        runtime.run(fail_job(self, org_id, job, "snapshot_too_large", message))
        return None
    seconds = time.monotonic() - started
    runtime.run(_finish(self, target, job, report, written=written, seconds=seconds))
    runtime.run(vacuum_chunks(target.repo_id))
    log.info(
        "files chunked",
        files=report.files,
        chunked=report.chunked,
        failed=report.failed,
        chunks=written,
    )
    return written
