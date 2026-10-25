"""The chunks stage: the snapshot's files cut into retrieval units and saved as ``chunks``.

``chunk_files`` runs after the symbols stage (a definition's chunks link to its node), on the
same working copy. Files are chunked in a pool of processes (:mod:`sensei_engine.chunk.stage`),
code parsed again there. Chunks are saved as they come, :data:`BATCH_ROWS` rows per
transaction, so a large repository's never sit in memory at once.

The stage starts by deleting the snapshot's chunks and clearing ``chunk_count``, so a run that
stopped part way starts over; keyword search serves a snapshot only once ``chunk_count`` is set,
at the end (which unlocks it). Then the new rows are ANALYZEd. Progress is reported in files, at
most once a second.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, CodeChunk, Repo, RepoFile, Snapshot, Stage
from sensei_engine.chunk.stage import ChunkReport, chunk_files, chunkable
from sensei_engine.chunk.store import BATCH_ROWS, FileChunks, save_chunks
from sensei_engine.snapshots import ArchiveTooLargeError
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.ingest.cache import repo_lock
from sensei_workers.jobstate import JobGoneError, job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.statistics import analyze
from sensei_workers.tasks.discover import SnapshotUnavailableError, local_worktree
from sensei_workers.tasks.ingest import IngestTask, fail_job

log = get_logger(__name__)

PROGRESS_EVERY_SECONDS = 1.0
STAGE_ORDER = list(Stage)


@dataclass(frozen=True, slots=True)
class Target:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str


async def _begin(task: SenseiTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or restart) the job's chunks stage, with the snapshot's old chunks gone; None if
    there is nothing to do."""
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
        snapshot.chunk_count = None  # not searchable while its chunks are rebuilt
        await db.execute(delete(CodeChunk).where(CodeChunk.snapshot_id == snapshot.id))
        return Target(repo.org_id, repo.id, snapshot.id, snapshot.commit_sha)


async def _files(task: SenseiTask, target: Target) -> dict[str, tuple[uuid.UUID, str | None]]:
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


async def _save(
    task: SenseiTask,
    target: Target,
    job_id: uuid.UUID,
    file_ids: dict[str, uuid.UUID],
    batch: list[FileChunks],
    *,
    progress: tuple[int, int],
) -> int:
    """Save a batch of files' chunks (its own transaction) and report how far the stage is."""
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(Stage.CHUNKS)
        snapshot = await scope.db.get(Snapshot, target.snapshot_id)
        if snapshot is None:
            raise JobGoneError("the snapshot is gone")
        written = await save_chunks(scope.db, snapshot, file_ids, batch, replace_all=False)
        scope.machine.update_progress(Stage.CHUNKS, *progress)
        return written


async def _progress(
    task: SenseiTask, target: Target, job_id: uuid.UUID, done: int, total: int
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(Stage.CHUNKS)
        scope.machine.update_progress(Stage.CHUNKS, done, total)


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
        snapshot = await scope.db.get(Snapshot, target.snapshot_id)
        if snapshot is None:
            raise JobGoneError("the snapshot is gone")
        snapshot.chunk_count = written
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
    file_ids = {path: file_id for path, (file_id, _) in files.items()}
    report = ChunkReport()
    batch: list[FileChunks] = []
    rows = written = 0
    started = last = time.monotonic()
    try:
        with repo_lock(runtime.settings.ingest.cache_dir, str(target.repo_id)):
            root = local_worktree(target)
            pairs = [(path, language) for path, (_, language) in files.items()]
            for chunked in chunk_files(root, pairs):
                report.add(chunked)
                if chunked.chunks:
                    batch.append(FileChunks(chunked.path, chunked.language, chunked.chunks))
                    rows += len(chunked.chunks)
                now = time.monotonic()
                if rows >= BATCH_ROWS:
                    progress = (report.files, len(files))
                    saving = _save(self, target, job, file_ids, batch, progress=progress)
                    written += runtime.run(saving)
                    batch, rows, last = [], 0, now
                elif now - last >= PROGRESS_EVERY_SECONDS:
                    last = now
                    runtime.run(_progress(self, target, job, report.files, len(files)))
    except SnapshotUnavailableError:
        message = "The snapshot of this commit is missing. Reindex the repository."
        runtime.run(fail_job(self, org_id, job, "snapshot_unavailable", message))
        return None
    except ArchiveTooLargeError:
        message = "The snapshot of this commit is larger than the size limit."
        runtime.run(fail_job(self, org_id, job, "snapshot_too_large", message))
        return None
    if batch:
        progress = (report.files, len(files))
        written += runtime.run(_save(self, target, job, file_ids, batch, progress=progress))
    seconds = time.monotonic() - started
    runtime.run(_finish(self, target, job, report, written=written, seconds=seconds))
    runtime.run(analyze("chunks"))
    log.info(
        "files chunked",
        files=report.files,
        chunked=report.chunked,
        failed=report.failed,
        chunks=written,
    )
    return written
