"""The graph stage (3.6.3): the snapshot's code graph (imports, calls, references, bases, ...)
resolved and written, which unlocks the graph view and callers.

``build_graph`` follows summaries (done or skipped; or whichever stage came last before it when
summaries, or embeddings too, passed themselves over for want of a key: :func:`previous_done`),
on the working copy the other stages used. Files of the languages a resolver reads are parsed
again in the pool and their facts extracted there; the project (paths, definitions, configs) is
read from the database; the resolvers run in this process, and the snapshot's CODE layer is
replaced in one transaction with the stage completed (:mod:`sensei_engine.graph.stage`), and the
graph in memory's artifact (``graph.bin``, :mod:`sensei_engine.graph.snapshot`) written with a
version the stage's details name.
Progress is reported in files, at most once a second. A run that stopped starts over; the write
replaces everything, so nothing is written twice.

The graph is a view of the code, not what an index stands on: a resolver that raises skips the
stage (with the reason) and the index still finishes. So does a snapshot with no file of a
language any resolver reads.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, IndexJob, Repo, RepoFile, Snapshot, Stage
from sensei_core.storage import Bucket, artifact_key
from sensei_engine.chunk.store import SnapshotKey
from sensei_engine.graph.snapshot import ARTIFACT_NAME, GraphSnapshot
from sensei_engine.graph.stage import (
    ExtractReport,
    extract_files,
    load_project,
    resolve_graph,
    resolver_for,
    write_graph,
)
from sensei_engine.ingest.classify import Flag
from sensei_engine.snapshots import ArchiveTooLargeError
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.ingest.cache import repo_lock
from sensei_workers.jobstate import JobScope, job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.statistics import analyze
from sensei_workers.tasks.discover import SnapshotUnavailableError, local_worktree
from sensei_workers.tasks.ingest import IngestTask, fail_job

log = get_logger(__name__)

STAGE = Stage.GRAPH
PROGRESS_EVERY_SECONDS = 1.0
STAGE_ORDER = list(Stage)
UNREAD_FLAGS = frozenset({Flag.BINARY, Flag.TOO_LARGE, Flag.LFS_POINTER})


@dataclass(frozen=True, slots=True)
class Target:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str


def previous_done(job: IndexJob) -> bool:
    """The stages before this one are over: summaries done or skipped; or, when the summaries
    stage passed itself over (no Anthropic key), embeddings done or skipped; or chunks done when
    the embeddings stage passed itself over too (no Voyage key)."""
    status = (job.stages or {}).get(job.stage.value if job.stage else "", {}).get("status")
    if job.stage in (Stage.SUMMARIES, Stage.EMBEDDINGS):
        return status in {"done", "skipped"}
    return job.stage is Stage.CHUNKS and status == "done"


async def _begin(task: SenseiTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or restart) the job's graph stage; None if there is nothing to do."""
    async with job_scope(task, org_id, job_id) as scope:
        db, job = scope.db, scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None
        ready = previous_done(job)
        resuming = (
            job.stage is STAGE
            and (job.stages or {}).get(STAGE.value, {}).get("status") == "running"
        )
        if not ready and not resuming:
            if job.stage is None or STAGE_ORDER.index(job.stage) < STAGE_ORDER.index(STAGE):
                raise NotYetError("the stages before the graph haven't finished")
            return None  # past this stage already (a duplicate delivery)
        repo = await db.get(Repo, job.repo_id)
        if repo is None or repo.deleted_at is not None:
            scope.machine.cancel()
            return None
        snapshot = await db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
        if snapshot is None:
            raise NotYetError("the job has no snapshot")
        if ready:
            scope.machine.advance(STAGE)
        return Target(repo.org_id, repo.id, snapshot.id, snapshot.commit_sha)


async def _files(task: SenseiTask, target: Target) -> list[tuple[str, str]]:
    """``(path, language)`` of the snapshot's files a resolver reads, in path order."""
    async with task.org_session(target.org_id) as db:
        rows = await db.execute(
            select(RepoFile.path, RepoFile.language, RepoFile.flags)
            .where(RepoFile.snapshot_id == target.snapshot_id)
            .order_by(RepoFile.path)
        )
        return [
            (path, language)
            for path, language, flags in rows
            if language is not None
            and resolver_for(language) is not None
            and not UNREAD_FLAGS.intersection(flags or ())
        ]


async def _progress(
    task: SenseiTask, target: Target, job_id: uuid.UUID, done: int, total: int
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        job = scope.require(STAGE)
        previous = int((job.stages or {}).get(STAGE.value, {}).get("done") or 0)
        scope.machine.update_progress(STAGE, max(done, previous), total)


def _skip(scope: JobScope, reason: str, why: str, **details: Any) -> None:
    scope.machine.skip(STAGE, reason=reason, **details)
    scope.machine.log(f"The code graph was skipped: {why}.", level="warn")


async def _skip_stage(
    task: SenseiTask, target: Target, job_id: uuid.UUID, reason: str, why: str
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(STAGE)
        _skip(scope, reason, why)


async def _build(
    task: SenseiTask,
    target: Target,
    job_id: uuid.UUID,
    *,
    root: Path,
    facts: Mapping[str, Mapping[str, Any]],
    report: ExtractReport,
    seconds: float,
) -> int | None:
    """Resolve and write the graph, and complete the stage; returns the edges written (None: a
    resolver failed, and the stage is skipped). Only resolving is caught: a resolver's bug costs
    the graph, not the index. Storage or the database failing propagates as in any task: for a
    moment (a timeout, a reset, a 5xx, a slow down: :func:`~sensei_workers.base.is_retryable`),
    the task is retried; otherwise the job fails."""
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(STAGE)
        project = await load_project(scope.db, target.snapshot_id, root)
        try:
            build = resolve_graph(project, facts)
        except Exception as exc:  # a resolver's bug (see the docstring)
            log.exception("graph resolution failed", job_id=str(job_id))
            _skip(scope, "graph_failed", f"resolving it failed ({type(exc).__name__})")
            return None
        snapshot = SnapshotKey(target.org_id, target.repo_id, target.snapshot_id)
        counts = await write_graph(scope.db, snapshot, build)
        # The graph in memory's artifact, read back from what this transaction wrote: an
        # artifact without its rows (the commit failed) is overwritten by the next attempt, and
        # the API only reads one whose version the job names.
        version = uuid.uuid4().hex
        graph = await GraphSnapshot.build(scope.db, target.snapshot_id, version=version)
        storage = await get_runtime().storage()
        key = artifact_key(target.org_id, target.repo_id, target.commit_sha, ARTIFACT_NAME)
        await storage.put_bytes(Bucket.ARTIFACTS, key, graph.dumps())
        machine = scope.machine
        total = max(report.files, 1)
        machine.update_progress(STAGE, total, total)
        machine.note(
            STAGE,
            files=report.files,
            extracted=report.extracted,
            failures=dict(sorted(report.failures.items())),
            examples={k: sorted(v) for k, v in sorted(report.examples.items())},
            nodes=counts.nodes,
            edges=counts.edges,
            unresolved_edges=counts.unresolved,
            resolvers=build.details,
            wall_seconds=round(seconds, 3),
            version=version,
        )
        machine.log(
            f"Linked {report.extracted:,} files: {counts.edges:,} imports, calls and references. "
            "The code graph is ready."
        )
        machine.complete(STAGE)
        return counts.edges


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.BUILD_GRAPH.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def build_graph(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> int | None:
    """Resolve and write the job's snapshot's code graph; returns how many edges were written."""
    runtime = get_runtime()
    job = uuid.UUID(job_id)
    target = runtime.run(_begin(self, org_id, job))
    if target is None:
        return None
    files = runtime.run(_files(self, target))
    if not files:
        why = "no file is in a language the graph reads yet"
        runtime.run(_skip_stage(self, target, job, "no_languages", why))
        return None
    report = ExtractReport()
    facts: dict[str, dict[str, Any]] = {}
    started = last = time.monotonic()
    try:
        with repo_lock(runtime.settings.ingest.cache_dir, str(target.repo_id)):
            root = local_worktree(target)
            for found in extract_files(root, files):
                report.add(found)
                if found.facts is not None:
                    facts.setdefault(found.resolver, {})[found.path] = found.facts
                now = time.monotonic()
                if now - last >= PROGRESS_EVERY_SECONDS:
                    last = now
                    runtime.run(_progress(self, target, job, report.files, len(files)))
            written = runtime.run(
                _build(
                    self,
                    target,
                    job,
                    root=root,
                    facts=facts,
                    report=report,
                    seconds=time.monotonic() - started,
                )
            )
    except SnapshotUnavailableError:
        message = "The snapshot of this commit is missing. Reindex the repository."
        runtime.run(fail_job(self, org_id, job, "snapshot_unavailable", message))
        return None
    except ArchiveTooLargeError:
        message = "The snapshot of this commit is larger than the size limit."
        runtime.run(fail_job(self, org_id, job, "snapshot_too_large", message))
        return None
    if written is None:
        return None
    runtime.run(analyze("edges"))  # traversals' plans need the new rows' statistics
    log.info("graph built", files=report.files, extracted=report.extracted, edges=written)
    return written
