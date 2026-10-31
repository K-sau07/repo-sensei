"""The links stage (3.12.4): the snapshot's cross-layer links (endpoints and their handlers,
frontend calls and the endpoints they target, models, tables and what reads and writes them),
which unlocks the trace, and with it diagrams, fixes and changes.

``build_links`` follows the graph stage (done or skipped), on the working copy the other stages
used. Python, JavaScript and TypeScript files are parsed again in the pool and every linker's
facts extracted there; the project (paths, definitions, configs) is read from the database and
the ``.env`` files from the working copy; when ``SENSEI_SANDBOX_INTROSPECT`` is on, the
repository's FastAPI apps are imported in the sandbox for their routes (else, or when that
fails, the static routes stand). The linkers run in this process and the snapshot's LINKS layer
is replaced in one transaction with the stage completed (:mod:`sensei_engine.link.stage`); the
graph in memory's artifact is written again with both layers, under a new version the stage's
details name (the API reads the newest finished one: the links', else the graph's).
Progress is reported in files, at most once a second. A run that stopped starts over; the write
replaces everything, so nothing is written twice.

Like the graph, the links are a view of the code: a linker that raises skips the stage (with the
reason) and the index still finishes, without a trace. So does a snapshot with no file a linker
reads. Only linking is caught: storage or the database failing propagates as in any task (for a
moment, :func:`~sensei_workers.base.is_retryable`: the task is retried; otherwise the job fails).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import orjson
from sqlalchemy import select

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, IndexJob, Repo, RepoFile, Snapshot, Stage
from sensei_core.storage import Bucket, artifact_key
from sensei_engine.chunk.store import SnapshotKey
from sensei_engine.graph.project import Project
from sensei_engine.graph.snapshot import ARTIFACT_NAME, GraphSnapshot
from sensei_engine.graph.stage import load_project
from sensei_engine.graph.writer import GraphCounts
from sensei_engine.incremental import Carry, changed_paths
from sensei_engine.ingest.classify import Flag
from sensei_engine.link import http_calls
from sensei_engine.link.fastapi import Route, RuntimeRoutes
from sensei_engine.link.stage import (
    FileLinks,
    LinkExtractReport,
    env_files,
    extract_links,
    links_language,
    resolve_links,
    runtime_routes,
    write_links,
)
from sensei_engine.parse.stage import read_source
from sensei_engine.sandbox.deps import DependencyImages
from sensei_engine.sandbox.runner import Sandbox
from sensei_engine.snapshots import ArchiveTooLargeError
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.incremental import carry_for
from sensei_workers.ingest.cache import repo_lock
from sensei_workers.jobstate import JobScope, job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.statistics import analyze
from sensei_workers.tasks.discover import SnapshotUnavailableError, local_worktree
from sensei_workers.tasks.graph import Target
from sensei_workers.tasks.ingest import IngestTask, fail_job

log = get_logger(__name__)

STAGE = Stage.LINKS
PROGRESS_EVERY_SECONDS = 1.0
# The running apps' routes, as the sandbox read them, kept with the snapshot: an incremental run
# that changed no Python file reads them from the last index rather than running the apps again.
ROUTES_ARTIFACT = "runtime-routes.json"
ROUTES_VERSION = 1  # of what it holds (a Route's fields): another version's isn't read
MAX_ROUTES_BYTES = 64 * 1024 * 1024
# What the running apps' routes depend on besides Python: their dependencies and configuration.
APP_INPUTS = (".py", ".pyi", ".toml", ".cfg", ".ini", ".txt", ".lock", ".yaml", ".yml", ".json")
APP_NAMES = ("Pipfile", "Dockerfile", ".env")
STAGE_ORDER = list(Stage)
UNREAD_FLAGS = frozenset({Flag.BINARY, Flag.TOO_LARGE, Flag.LFS_POINTER})


def graph_over(job: IndexJob) -> bool:
    """The graph stage is over: done, or skipped (the links don't need the code graph)."""
    status = (job.stages or {}).get(Stage.GRAPH.value, {}).get("status")
    return job.stage is Stage.GRAPH and status in {"done", "skipped"}


async def _begin(task: SenseiTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or restart) the job's links stage; None if there is nothing to do."""
    async with job_scope(task, org_id, job_id) as scope:
        db, job = scope.db, scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None
        ready = graph_over(job)
        resuming = (
            job.stage is STAGE
            and (job.stages or {}).get(STAGE.value, {}).get("status") == "running"
        )
        if not ready and not resuming:
            if job.stage is None or STAGE_ORDER.index(job.stage) < STAGE_ORDER.index(STAGE):
                raise NotYetError("the stages before the links haven't finished")
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


async def _files(task: SenseiTask, target: Target) -> tuple[list[tuple[str, str]], list[str]]:
    """``(path, language)`` of the snapshot's files a linker reads, in path order; and every
    path (the ``.env`` files are among them)."""
    async with task.org_session(target.org_id) as db:
        rows = await db.execute(
            select(RepoFile.path, RepoFile.language, RepoFile.flags)
            .where(RepoFile.snapshot_id == target.snapshot_id)
            .order_by(RepoFile.path)
        )
        every = list(rows)
    files = [
        (path, language)
        for path, language, flags in every
        if links_language(language) and not UNREAD_FLAGS.intersection(flags or ())
    ]
    return files, [path for path, _, _ in every]


def _envs(root: Path, paths: list[str]) -> dict[str, dict[str, str]]:
    """The ``.env`` values by folder (one that can't be read is left out)."""
    texts: dict[str, str] = {}
    for path in env_files(paths):
        try:
            texts[path] = read_source(root, path).decode("utf-8", "replace")
        except OSError:
            continue
    return http_calls.env_values(Project(frozenset(paths)), texts)


async def _progress(
    task: SenseiTask, target: Target, job_id: uuid.UUID, done: int, total: int
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        job = scope.require(STAGE)
        previous = int((job.stages or {}).get(STAGE.value, {}).get("done") or 0)
        scope.machine.update_progress(STAGE, max(done, previous), total)


def _skip(scope: JobScope, reason: str, why: str) -> None:
    scope.machine.skip(STAGE, reason=reason)
    scope.machine.log(f"The cross-layer links were skipped: {why}.", level="warn")


async def _skip_stage(
    task: SenseiTask, target: Target, job_id: uuid.UUID, reason: str, why: str
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(STAGE)
        _skip(scope, reason, why)


async def _runtime(
    task: SenseiTask, target: Target, root: Path, files: Mapping[str, FileLinks]
) -> RuntimeRoutes | None:
    """The running apps' routes, when the sandbox may read them (else None: static only). A
    sandbox that can't run is the routes' limitation, never the stage's failure."""
    settings = get_runtime().settings.sandbox
    if not settings.introspect:
        return None
    async with task.org_session(target.org_id) as db:
        project = await load_project(db, target.snapshot_id, root)
    sandbox = Sandbox.from_settings(settings)
    images = DependencyImages.from_settings(sandbox, settings)
    try:
        return await runtime_routes(sandbox, images, root, project, files)
    except Exception as exc:  # the sandbox's trouble costs the runtime routes, not the stage
        log.warning("route introspection failed", error=type(exc).__name__)
        return RuntimeRoutes([], limitation=f"the sandbox failed ({type(exc).__name__})")


def routes_json(routes: RuntimeRoutes) -> bytes:
    return orjson.dumps({"version": ROUTES_VERSION, **asdict(routes)})


def routes_from(data: bytes) -> RuntimeRoutes | None:
    """What :func:`routes_json` wrote (None: another version's, or unreadable)."""
    try:
        return _routes(cast("dict[str, Any]", orjson.loads(data)))
    except (orjson.JSONDecodeError, TypeError, KeyError, ValueError):
        return None


def _routes(raw: dict[str, Any]) -> RuntimeRoutes | None:
    if raw.get("version") != ROUTES_VERSION:
        return None
    routes: list[Route] = []
    for found in cast("list[dict[str, Any]]", raw.get("routes", [])):
        fields = dict(found)
        fields["tags"] = tuple(fields.get("tags", ()))
        fields["unresolved"] = tuple(fields.get("unresolved", ()))
        routes.append(Route(**fields))
    return RuntimeRoutes(
        routes,
        limitation=raw.get("limitation"),
        notes=list(raw.get("notes", [])),
        detail=str(raw.get("detail", "")),
    )


def introspect_again(changed: set[str]) -> bool:
    """Whether an incremental run reads the apps' routes again: a Python file (a router, an
    app, or what they import), a dependency manifest or a configuration file was added,
    changed, moved or deleted."""
    return any(
        path.endswith(APP_INPUTS) or path.rsplit("/", 1)[-1].startswith(APP_NAMES)
        for path in changed
    )


async def _kept_routes(task: SenseiTask, target: Target, carry: Carry) -> RuntimeRoutes | None:
    """The last index's runtime routes, when this incremental run changed no Python file
    (None: it did, or there are none kept: the apps are read again, when they may be)."""
    async with task.org_session(target.org_id) as db:
        snapshot = await db.get(Snapshot, target.snapshot_id)
        if snapshot is None or introspect_again(await changed_paths(db, snapshot, carry)):
            return None
    storage = await get_runtime().storage()
    key = artifact_key(target.org_id, target.repo_id, carry.previous_sha, ROUTES_ARTIFACT)
    data = await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=MAX_ROUTES_BYTES)
    return routes_from(data) if data is not None else None


async def _keep_routes(target: Target, routes: RuntimeRoutes) -> None:
    storage = await get_runtime().storage()
    key = artifact_key(target.org_id, target.repo_id, target.commit_sha, ROUTES_ARTIFACT)
    await storage.put_bytes(
        Bucket.ARTIFACTS, key, routes_json(routes), content_type="application/json"
    )


async def _build(
    task: SenseiTask,
    target: Target,
    job_id: uuid.UUID,
    *,
    root: Path,
    files: Mapping[str, FileLinks],
    runtime: RuntimeRoutes | None,
    report: LinkExtractReport,
    seconds: float,
    routes_kept: bool = False,
) -> GraphCounts | None:
    """Resolve and write the links, the graph artifact again, and complete the stage (None: a
    linker failed, and the stage is skipped; see the module)."""
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(STAGE)
        project = await load_project(scope.db, target.snapshot_id, root)
        try:
            build = resolve_links(project, files, runtime)
        except Exception as exc:  # a linker's bug costs the trace, not the index
            log.exception("linking failed", job_id=str(job_id))
            _skip(scope, "links_failed", f"linking failed ({type(exc).__name__})")
            return None
        snapshot = SnapshotKey(target.org_id, target.repo_id, target.snapshot_id)
        counts = await write_links(scope.db, snapshot, build)
        # Both layers in the graph in memory: its artifact, read back from this transaction (see
        # the graph stage), under a version only this stage's details name.
        version = uuid.uuid4().hex
        graph = await GraphSnapshot.build(scope.db, target.snapshot_id, version=version)
        storage = await get_runtime().storage()
        key = artifact_key(target.org_id, target.repo_id, target.commit_sha, ARTIFACT_NAME)
        await storage.put_bytes(Bucket.ARTIFACTS, key, graph.dumps())
        machine = scope.machine
        total = max(report.files, 1)
        machine.update_progress(STAGE, total, total)
        routes = build.details.get("routes", {})
        urls = build.details.get("urls", {})
        machine.note(
            STAGE,
            files=report.files,
            extracted=report.extracted,
            failures=dict(sorted(report.failures.items())),
            examples={k: sorted(v) for k, v in sorted(report.examples.items())},
            nodes=counts.nodes,
            edges=counts.edges,
            unresolved_edges=counts.unresolved,
            linkers=build.details,
            introspected=runtime is not None,
            **({"routes_kept": True} if routes_kept else {}),
            wall_seconds=round(seconds, 3),
            version=version,
        )
        machine.log(
            f"Linked the layers: {routes.get('endpoints', 0):,} endpoints, "
            f"{urls.get('linked', 0):,} of {urls.get('calls', 0):,} frontend calls matched to "
            "them. Tracing is ready."
        )
        machine.complete(STAGE)
        return counts


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.BUILD_LINKS.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def build_links(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> int | None:
    """Link the job's snapshot across its layers; returns how many edges were written."""
    runtime = get_runtime()
    job = uuid.UUID(job_id)
    target = runtime.run(_begin(self, org_id, job))
    if target is None:
        return None
    files, paths = runtime.run(_files(self, target))
    if not files:
        why = "no file is in a language the linkers read yet"
        runtime.run(_skip_stage(self, target, job, "no_languages", why))
        return None
    report = LinkExtractReport()
    found: dict[str, FileLinks] = {}
    started = last = time.monotonic()
    try:
        with repo_lock(runtime.settings.ingest.cache_dir, str(target.repo_id)):
            root = local_worktree(target)
            for facts in extract_links(root, files, _envs(root, paths)):
                report.add(facts)
                found[facts.path] = facts
                now = time.monotonic()
                if now - last >= PROGRESS_EVERY_SECONDS:
                    last = now
                    runtime.run(_progress(self, target, job, report.files, len(files)))
            # An incremental run that changed no Python file keeps the last index's runtime
            # routes (5.3): running the apps again would read the same ones.
            carry = runtime.run(carry_for(self, target.org_id, job, target.snapshot_id))
            routes = runtime.run(_kept_routes(self, target, carry)) if carry else None
            kept = routes is not None
            if not kept:
                routes = runtime.run(_runtime(self, target, root, found))
            if routes is not None and routes.limitation is None:  # a sandbox's failure isn't kept
                runtime.run(_keep_routes(target, routes))
            counts = runtime.run(
                _build(
                    self,
                    target,
                    job,
                    root=root,
                    files=found,
                    runtime=routes,
                    report=report,
                    seconds=time.monotonic() - started,
                    routes_kept=kept,
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
    if counts is None:
        return None
    runtime.run(analyze("edges"))
    log.info("links built", files=report.files, extracted=report.extracted, edges=counts.edges)
    return counts.edges
