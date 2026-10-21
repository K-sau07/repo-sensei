"""The discover stage: which files of the snapshot get indexed, and what each one is.

``discover_files`` runs after the clone stage, on the working copy the clone left in the cache.
A worker that doesn't have it (another machine took the clone) unpacks the snapshot archive from
object storage instead. The file list, the snapshot summary and the repository's languages are
written in one transaction (see :mod:`sensei_engine.ingest.store`), which completes the stage.

The constellation forms as files are found: once the walk has listed them, their layout is
known (:class:`~sensei_engine.ingest.constellation.DirectoryLayout`); each batch of files read
goes to Redis as points, and a ``constellation`` event tells viewers how far they can read. The
whole constellation is stored with the snapshot's artifacts in the same step that saves the
files, so its points name the saved rows.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    Repo,
    Snapshot,
    SnapshotStatus,
    Stage,
)
from sensei_core.repos.service import RepoSettings
from sensei_core.storage import Bucket, artifact_key, snapshot_key
from sensei_engine.ingest import constellation
from sensei_engine.ingest.classify import FileChangedError
from sensei_engine.ingest.constellation import (
    Constellation,
    DirectoryLayout,
    LiveConstellation,
    Point,
)
from sensei_engine.ingest.discover import DiscoveredFile, Discovery, discover
from sensei_engine.ingest.store import save_discovery
from sensei_engine.ingest.walk import TooManyFilesError
from sensei_engine.snapshots import ArchiveTooLargeError, extract_archive
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, RetryableError, SenseiTask
from sensei_workers.ingest.cache import repo_lock
from sensei_workers.ingest.git import CHECKED_OUT_MARKER
from sensei_workers.jobstate import JobGoneError, job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.tasks.ingest import IngestTask, fail_job

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Target:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str
    ignore_globs: tuple[str, ...]


class SnapshotUnavailableError(Exception):
    """Neither the working copy nor the archive is there."""


async def _begin(task: SenseiTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or resume) the job's discover stage; None if there is nothing to do."""
    async with job_scope(task, org_id, job_id) as scope:
        db, job = scope.db, scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None  # finished, failed or cancelled meanwhile
        stages = job.stages or {}
        machine = scope.machine
        clone_done = job.stage is Stage.CLONE and stages.get("clone", {}).get("status") == "done"
        resuming = job.stage is Stage.DISCOVER and stages["discover"].get("status") == "running"
        if not clone_done and not resuming:
            if job.stage is None or job.stage is Stage.CLONE:
                raise NotYetError("the clone stage hasn't finished")
            return None  # past this stage already (a duplicate delivery)
        repo = await db.get(Repo, job.repo_id)
        if repo is None or repo.deleted_at is not None:
            machine.cancel()
            return None
        snapshot = await db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
        if snapshot is None or snapshot.status is not SnapshotStatus.READY:
            raise NotYetError("the snapshot isn't ready")
        if clone_done:
            machine.advance(Stage.DISCOVER)  # only once everything it needs is there
        settings = RepoSettings.model_validate(repo.settings or {})
        return Target(
            org_id=repo.org_id,
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            commit_sha=snapshot.commit_sha,
            ignore_globs=tuple(settings.ignore_globs),
        )


def _checked_out(worktree: Path) -> str | None:
    """The commit a cached working copy holds, once its checkout finished (not ``HEAD``: that
    moves before the files are all written)."""
    marker = worktree / ".git" / CHECKED_OUT_MARKER
    try:
        if marker.is_symlink():
            return None
        return marker.read_text().strip()
    except OSError:
        return None


async def _download(target: Target, out: Path) -> None:
    storage = await get_runtime().storage()
    key = snapshot_key(target.org_id, target.repo_id, target.commit_sha)
    if await storage.size(Bucket.SNAPSHOTS, key) is None:
        raise SnapshotUnavailableError(key)
    with out.open("wb") as handle:
        async for chunk in storage.get_stream(Bucket.SNAPSHOTS, key):
            handle.write(chunk)


def _worktree(target: Target) -> Path:
    """The snapshot's files on local disk: the cached working copy, or the archive unpacked in
    its place."""
    runtime = get_runtime()
    settings = runtime.settings.ingest
    worktree = settings.cache_dir / str(target.repo_id)
    if _checked_out(worktree) == target.commit_sha:
        return worktree
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".unpack-", dir=settings.cache_dir))
    try:
        archive = scratch / "snapshot.tar.zst"
        runtime.run(_download(target, archive))
        unpacked = scratch / "tree"
        extract_archive(archive, unpacked, max_bytes=settings.max_checkout_bytes)
        shutil.rmtree(worktree, ignore_errors=True)
        unpacked.rename(worktree)
        # An unpacked copy has no .git: record the commit the same way a checkout does.
        (worktree / ".git").mkdir()
        (worktree / ".git" / CHECKED_OUT_MARKER).write_text(target.commit_sha)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return worktree


class Sky:
    """The run's constellation while discovery reads the files: its layout once the walk has
    listed them, and the points sent so far."""

    def __init__(self, job_id: uuid.UUID) -> None:
        self.job_id = job_id
        self._live: LiveConstellation | None = None

    @property
    def live(self) -> LiveConstellation:
        if self._live is None:
            raise RuntimeError("the walk hasn't listed the files yet")
        return self._live

    @property
    def started(self) -> bool:
        return self._live is not None

    async def begin(self, paths: Sequence[str]) -> None:
        live = LiveConstellation(
            get_runtime().redis, self.job_id, DirectoryLayout(paths), uuid.uuid4().hex
        )
        await live.begin()  # an earlier attempt's points go
        self._live = live

    def points(self, files: Sequence[DiscoveredFile]) -> list[Point]:
        layout = self.live.layout
        return [
            layout.point(
                file_id=file.id,
                path=file.path,
                role=file.role.value,
                language=file.language,
                size=file.size,
                loc=file.loc,
            )
            for file in files
        ]

    async def add(self, files: Sequence[DiscoveredFile], *, last: bool) -> None:
        await self.live.add(self.points(files))
        if last:
            await self.live.finish()


async def _progress(
    task: SenseiTask,
    org_id: str,
    job_id: uuid.UUID,
    done: int,
    total: int,
    *,
    sky: LiveConstellation | None = None,
) -> None:
    """Discover progress, and how far the constellation can be read; stops the walk if the job
    was cancelled meanwhile."""
    async with job_scope(task, org_id, job_id) as scope:
        job = scope.require(Stage.DISCOVER)
        previous = int((job.stages or {}).get("discover", {}).get("done") or 0)
        scope.machine.update_progress(Stage.DISCOVER, max(done, previous), total)
        if sky is not None and sky.cursor > 0:
            scope.outbox.constellation(
                job, cursor=sky.cursor, total=sky.total, generation=sky.generation
            )


async def _store_constellation(target: Target, sky: Sky, found: Discovery) -> int:
    storage = await get_runtime().storage()
    live = sky.live
    data = Constellation.build(live.generation, live.layout, sky.points(found.files)).to_bytes()
    name = constellation.ARTIFACT_NAME
    key = artifact_key(target.org_id, target.repo_id, target.commit_sha, name)
    await storage.put_bytes(Bucket.ARTIFACTS, key, data, content_type="application/zstd")
    return len(data)


async def _save(
    task: SenseiTask, target: Target, job_id: uuid.UUID, found: Discovery, sky: Sky
) -> bool:
    """Write the discovery and its constellation and complete the stage (stops quietly if the
    job moved on)."""
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(Stage.DISCOVER)
        snapshot = await scope.db.get(Snapshot, target.snapshot_id)
        if snapshot is None:
            raise JobGoneError("the snapshot is gone")
        rows = await save_discovery(scope.db, snapshot, found)
        # Stored while the job row is locked (a cancel can't come in between) and before the
        # commit (the stage is never done without it); its points carry the rows' ids.
        stored = await _store_constellation(target, sky, found)
        summary = snapshot.summary or {}
        machine = scope.machine
        total = max(rows, 1)
        machine.update_progress(Stage.DISCOVER, total, total)
        machine.note(
            Stage.DISCOVER,
            files=rows,
            loc=found.loc,
            frameworks=sorted({f.name for f in found.frameworks}),
            # For the live view: bytes per language (GitHub's names) and Compose services.
            languages=found.github_languages(),
            services=sorted({s for f in found.frameworks for s in f.details.get("services", [])}),
            skipped=summary.get("skipped", {}),
            invalid_ignore_globs=summary.get("invalid_ignore_globs", []),
            constellation={
                "generation": sky.live.generation,
                "points": rows,
                "directories": len(sky.live.layout.dirs),
                "bytes": stored,
            },
        )
        machine.log(
            f"Found {rows:,} files to index ({found.loc:,} lines)"
            + (
                f"; frameworks: {', '.join(sorted({f.name for f in found.frameworks}))}."
                if found.frameworks
                else "."
            )
        )
        for glob in summary.get("invalid_ignore_globs", []):
            machine.log(f"Ignore pattern {glob!r} isn't valid and was skipped.", level="warn")
        machine.complete(Stage.DISCOVER)
        return True


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.DISCOVER_FILES.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def discover_files(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> int | None:
    """Discover the files of the job's snapshot; returns how many are indexed."""
    runtime = get_runtime()
    job = uuid.UUID(job_id)
    target = runtime.run(_begin(self, org_id, job))
    if target is None:
        return None
    settings = runtime.settings.ingest
    sky = Sky(job)

    def walked(paths: Sequence[str]) -> None:
        runtime.run(sky.begin(paths))

    def progress(done: int, total: int, batch: Sequence[DiscoveredFile]) -> None:
        runtime.run(sky.add(batch, last=done == total))
        runtime.run(_progress(self, org_id, job, done, total, sky=sky.live))

    try:
        with repo_lock(settings.cache_dir, str(target.repo_id)):  # no clone or eviction meanwhile
            found = discover(
                _worktree(target),
                ignore_globs=target.ignore_globs,
                max_files=settings.max_files,
                walked=walked,
                progress=progress,
            )
    except TooManyFilesError as exc:
        message = (
            f"The repository has more than {exc.limit:,} files to index. Add ignore patterns "
            "in the repository settings to leave generated or vendored code out."
        )
        runtime.run(fail_job(self, org_id, job, "too_many_files", message))
        return None
    except SnapshotUnavailableError:
        log.warning("snapshot unavailable")
        message = "The snapshot of this commit is missing. Reindex the repository."
        runtime.run(fail_job(self, org_id, job, "snapshot_unavailable", message))
        return None
    except ArchiveTooLargeError:
        log.warning("snapshot archive too large to unpack")
        message = "The snapshot of this commit is larger than the size limit."
        runtime.run(fail_job(self, org_id, job, "snapshot_too_large", message))
        return None
    except FileChangedError as exc:
        raise RetryableError("the working copy changed while it was read") from exc
    if not runtime.run(_save(self, target, job, found, sky)):
        return None
    runtime.run(sky.live.done())
    log.info("files discovered", files=len(found.files), frameworks=len(found.frameworks))
    return len(found.files)
