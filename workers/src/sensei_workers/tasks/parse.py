"""The parse and symbols stages: every file of the snapshot we have a grammar for, parsed with
tree-sitter, and its definitions saved as ``nodes``.

``parse_files`` runs after merkle, on the working copy discover used (or the snapshot archive,
unpacked, on another worker). Files are parsed in a pool of processes, and their symbols
extracted there (:mod:`sensei_engine.parse.stage`): syntax errors, timeouts and files the parser
can't handle are counted in the stage's details, never fatal. Progress is reported in files, at
most once a second.

The symbols stage follows in the same task (the trees stay in the pool's processes; the symbols
come back): the snapshot's nodes are replaced in one transaction and ``symbol_count`` set, which
unlocks go to definition. A run that stopped during either stage parses again.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, Repo, RepoFile, Snapshot, Stage
from sensei_engine.ingest.classify import Flag
from sensei_engine.parse.stage import ParseReport, parse_files, parseable
from sensei_engine.snapshots import ArchiveTooLargeError
from sensei_engine.symbols.store import FileSymbols, save_symbols
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
# Files whose content was never read as text: nothing to parse.
UNPARSEABLE_FLAGS = frozenset({Flag.BINARY, Flag.TOO_LARGE, Flag.LFS_POINTER})


@dataclass(frozen=True, slots=True)
class Target:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str


async def _begin(task: SenseiTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or resume) the job's parse stage; None if there is nothing to do."""
    async with job_scope(task, org_id, job_id) as scope:
        db, job = scope.db, scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None
        stages = job.stages or {}
        machine = scope.machine
        merkle_done = job.stage is Stage.MERKLE and stages.get("merkle", {}).get("status") == "done"
        resuming = (
            job.stage in (Stage.PARSE, Stage.SYMBOLS)
            and stages.get(str(job.stage), {}).get("status") == "running"
        )
        if not merkle_done and not resuming:
            if job.stage in (None, Stage.CLONE, Stage.DISCOVER, Stage.MERKLE):
                raise NotYetError("the merkle stage hasn't finished")
            return None  # past these stages already (a duplicate delivery)
        repo = await db.get(Repo, job.repo_id)
        if repo is None or repo.deleted_at is not None:
            machine.cancel()
            return None
        snapshot = await db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
        if snapshot is None:
            raise NotYetError("the job has no snapshot yet")
        if merkle_done:
            machine.advance(Stage.PARSE)
        return Target(repo.org_id, repo.id, snapshot.id, snapshot.commit_sha)


async def _files(task: SenseiTask, target: Target) -> dict[str, tuple[uuid.UUID, str]]:
    """Path to ``(file id, language)`` for the snapshot's files we parse, in path order."""
    async with task.org_session(target.org_id) as db:
        rows = await db.execute(
            select(RepoFile.path, RepoFile.id, RepoFile.language, RepoFile.flags)
            .where(RepoFile.snapshot_id == target.snapshot_id)
            .order_by(RepoFile.path)
        )
        return {
            path: (file_id, language)
            for path, file_id, language, flags in rows
            if language is not None
            and parseable(language)
            and not UNPARSEABLE_FLAGS.intersection(flags or ())
        }


async def _progress(
    task: SenseiTask, org_id: str, job_id: uuid.UUID, done: int, total: int
) -> None:
    async with job_scope(task, org_id, job_id) as scope:
        job = scope.require(Stage.PARSE, Stage.SYMBOLS)
        if job.stage is not Stage.PARSE:
            return  # parsing again to resume the symbols stage: nothing to report
        previous = int((job.stages or {}).get("parse", {}).get("done") or 0)
        scope.machine.update_progress(Stage.PARSE, max(done, previous), total)


async def _finish_parse(
    task: SenseiTask, target: Target, job_id: uuid.UUID, report: ParseReport, seconds: float
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        job = scope.require(Stage.PARSE, Stage.SYMBOLS)
        if job.stage is not Stage.PARSE:
            return  # resuming the symbols stage: parse was recorded the first time
        machine = scope.machine
        total = max(report.files, 1)
        machine.update_progress(Stage.PARSE, total, total)
        machine.note(Stage.PARSE, **report.details(), wall_seconds=round(seconds, 3))
        machine.log(
            f"Parsed {report.parsed:,} of {report.files:,} source files"
            + (
                f"; {report.with_errors:,} with syntax errors (parsed around them)."
                if report.with_errors
                else "."
            )
        )
        skipped = report.files - report.parsed
        if skipped:
            machine.log(
                f"{skipped:,} files couldn't be parsed (timed out, unreadable or failed).",
                level="warn",
            )
        machine.advance(Stage.SYMBOLS)


async def _save(
    task: SenseiTask,
    target: Target,
    job_id: uuid.UUID,
    files: dict[str, tuple[uuid.UUID, str]],
    found: list[FileSymbols],
) -> int:
    """Replace the snapshot's symbols and complete the symbols stage."""
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(Stage.SYMBOLS)
        snapshot = await scope.db.get(Snapshot, target.snapshot_id)
        if snapshot is None:
            raise JobGoneError("the snapshot is gone")
        ids = {path: file_id for path, (file_id, _) in files.items()}
        written = await save_symbols(scope.db, snapshot, ids, found)
        snapshot.symbol_count = written
        machine = scope.machine
        total = max(written, 1)
        machine.update_progress(Stage.SYMBOLS, total, total)
        kinds: dict[str, int] = {}
        for file in found:
            for symbol in file.symbols:
                kinds[symbol.kind.value] = kinds.get(symbol.kind.value, 0) + 1
        machine.note(Stage.SYMBOLS, symbols=written, kinds=dict(sorted(kinds.items())))
        machine.log(f"Found {written:,} definitions. Go to definition is ready.")
        machine.complete(Stage.SYMBOLS)
        return written


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.PARSE_FILES.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def parse_snapshot(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> int | None:
    """Parse the job's snapshot and save its symbols; returns how many symbols were saved."""
    runtime = get_runtime()
    job = uuid.UUID(job_id)
    target = runtime.run(_begin(self, org_id, job))
    if target is None:
        return None
    files = runtime.run(_files(self, target))
    report = ParseReport()
    found: list[FileSymbols] = []
    started = last = time.monotonic()
    try:
        with repo_lock(runtime.settings.ingest.cache_dir, str(target.repo_id)):
            root = local_worktree(target)
            pairs = [(path, language) for path, (_, language) in files.items()]
            for parsed in parse_files(root, pairs):
                report.add(parsed)
                if parsed.symbols:
                    found.append(FileSymbols(parsed.path, parsed.language, parsed.symbols))
                now = time.monotonic()
                if now - last >= PROGRESS_EVERY_SECONDS:
                    last = now
                    runtime.run(_progress(self, org_id, job, report.files, len(files)))
    except SnapshotUnavailableError:
        message = "The snapshot of this commit is missing. Reindex the repository."
        runtime.run(fail_job(self, org_id, job, "snapshot_unavailable", message))
        return None
    except ArchiveTooLargeError:
        message = "The snapshot of this commit is larger than the size limit."
        runtime.run(fail_job(self, org_id, job, "snapshot_too_large", message))
        return None
    runtime.run(_finish_parse(self, target, job, report, time.monotonic() - started))
    written = runtime.run(_save(self, target, job, files, found))
    runtime.run(analyze("nodes"))  # the lookups' plans need the new rows' statistics
    log.info(
        "files parsed",
        files=report.files,
        parsed=report.parsed,
        errors=report.with_errors,
        symbols=written,
    )
    return written
