"""The clone stage: a repository's code at one commit, archived to object storage.

``clone_snapshot`` resolves the branch to a commit through the GitHub API, checks the commit out
into the working-copy cache (see :mod:`sensei_workers.ingest.git`), archives it to the snapshots
bucket (reused when that commit was archived before), records the snapshot and completes the
job's ``clone`` stage. Problems that won't go away (too large, not found, no access) fail the job
with a reason the UI can show; network trouble is retried.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from sensei_core.github.client import Credential, GitHubError, RateLimited, as_object
from sensei_core.indexing.jobs import settle_repo
from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    GitHubInstallation,
    IndexJob,
    JobStatus,
    Repo,
    RepoSource,
    RepoStatus,
    Snapshot,
    SnapshotStatus,
    Stage,
)
from sensei_core.storage import Bucket, snapshot_key
from sensei_workers.app import celery_app
from sensei_workers.base import RetryableError, SenseiTask, is_retryable
from sensei_workers.ingest.archive import write_archive
from sensei_workers.ingest.cache import evict, repo_lock
from sensei_workers.ingest.git import CloneError, CloneSource, GitRunner, TransientGitError
from sensei_workers.jobstate import JobGoneError, job_scope
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)

# Clone stage progress, in steps: resolved, fetched, archived, uploaded, recorded.
STEPS = 5
# What an installation token for cloning may do: read this one repository, nothing else.
CLONE_PERMISSIONS = {"contents": "read", "metadata": "read"}
# GitHub's repository size counts the history too; far beyond the checkout limit, don't even
# start fetching.
HISTORY_SIZE_FACTOR = 10

INTERNAL_MESSAGE = (
    "Indexing stopped unexpectedly. Try again; if it keeps happening, contact support with the "
    "job id."
)
UNAVAILABLE_MESSAGE = "GitHub or our storage didn't answer for a while. Try again later."
TIMEOUT_MESSAGE = "A stage of indexing took too long and was stopped."
WORKER_LOST_MESSAGE = (
    "The worker indexing this repository kept stopping (it may be too large to index). Contact "
    "support with the job id."
)
ACCESS_LOST_MESSAGE = (
    "repo-sensei can't read this repository on GitHub any more (the GitHub App was removed from "
    "it, or it was deleted)."
)
NOT_PUBLIC_MESSAGE = "This repository isn't public on GitHub any more (or was deleted)."


@dataclass(frozen=True, slots=True)
class Target:
    repo_id: uuid.UUID
    org_id: uuid.UUID
    full_name: str
    ref: str
    source: RepoSource
    github_repo_id: int
    installation_id: int | None  # GitHub's id, for an installation token
    size_kb: int  # as GitHub reported it at connect time (history included)


def _now() -> datetime:
    return datetime.now(UTC)


class IngestTask(SenseiTask):
    """Ingest tasks fail their index job when they give up: out of retries, killed at the time
    limit, or dead-lettered after the worker kept dying (out of memory)."""

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        super().on_failure(exc, task_id, args, kwargs, einfo)
        if is_retryable(exc):  # out of retries on something temporary
            self._give_up(kwargs, "github_unavailable", UNAVAILABLE_MESSAGE)
        else:
            self._give_up(kwargs)

    def on_hard_timeout(self, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        super().on_hard_timeout(task_id, args, kwargs)
        self._give_up(kwargs, "stage_timeout", TIMEOUT_MESSAGE)

    def on_dead_letter(self, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        super().on_dead_letter(task_id, args, kwargs)
        self._give_up(kwargs, "worker_lost", WORKER_LOST_MESSAGE)

    def _give_up(
        self, kwargs: dict[str, Any], code: str = "internal_error", message: str = INTERNAL_MESSAGE
    ) -> None:
        job_id, org_id = kwargs.get("job_id"), kwargs.get("org_id")
        if not job_id or not org_id:
            return
        try:
            get_runtime().run(fail_job(self, org_id, uuid.UUID(str(job_id)), code, message))
        except Exception:
            log.exception("could not mark the index job failed")


async def fail_job(
    task: SenseiTask, org_id: str, job_id: uuid.UUID, code: str, message: str
) -> None:
    async with job_scope(task, org_id, job_id) as scope:
        job = scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return
        machine = scope.machine
        if job.status is JobStatus.QUEUED:
            machine.start()
        stage = job.stage or Stage.CLONE
        if (job.stages or {}).get(stage.value, {}).get("status") != "running":
            # Between stages: the failure belongs to the stage that would have run next.
            following = list(Stage)[list(Stage).index(stage) + 1 : list(Stage).index(stage) + 2]
            if following:
                machine.advance(following[0])
                stage = following[0]
        machine.fail(stage, {"code": code, "message": message})
        if job.snapshot_id is not None:
            snapshot = await scope.db.get(Snapshot, job.snapshot_id)
            if snapshot is not None and snapshot.status is SnapshotStatus.PENDING:
                snapshot.status = SnapshotStatus.FAILED
        repo = await scope.db.get(Repo, job.repo_id)
        if repo is not None:
            settle_repo(repo, JobStatus.FAILED)


class AccessLostError(CloneError):
    """GitHub no longer lets us read the repository: it is marked ``access_lost``."""


async def _access_lost(task: SenseiTask, target: Target) -> None:
    async with task.org_session(target.org_id) as db:
        repo = await db.get(Repo, target.repo_id)
        if repo is not None and repo.deleted_at is None:
            repo.status = RepoStatus.ACCESS_LOST


async def _begin(task: SenseiTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or resume) the job's clone stage; None if there is nothing to do."""
    async with job_scope(task, org_id, job_id) as scope:
        job = scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None
        repo = await scope.db.get(Repo, job.repo_id)
        machine = scope.machine
        if repo is None or repo.deleted_at is not None:
            machine.cancel()
            return None
        if job.status is JobStatus.QUEUED:
            machine.start(Stage.CLONE)
            if repo.status is not RepoStatus.ACCESS_LOST:
                repo.status = RepoStatus.INDEXING
        elif job.stage is not Stage.CLONE or job.stages["clone"].get("status") != "running":
            return None  # past this stage already (a duplicate delivery)
        installation = (
            await scope.db.get(GitHubInstallation, repo.installation_id)
            if repo.installation_id
            else None
        )
        return Target(
            repo_id=repo.id,
            org_id=repo.org_id,
            full_name=repo.full_name,
            ref=repo.default_branch,
            source=repo.source,
            github_repo_id=repo.github_repo_id,
            installation_id=installation.installation_id if installation else None,
            size_kb=repo.size_kb,
        )


async def _step(
    task: SenseiTask, org_id: str, job_id: uuid.UUID, done: int, *, log_line: str | None = None
) -> None:
    """Clone progress; stops the task if the job was cancelled meanwhile."""
    async with job_scope(task, org_id, job_id) as scope:
        job = scope.require(Stage.CLONE)
        previous = int((job.stages or {}).get("clone", {}).get("done") or 0)
        scope.machine.update_progress(Stage.CLONE, max(done, previous), STEPS)
        if log_line:
            scope.machine.log(log_line)


async def _token(target: Target) -> str | None:
    """A token that can read only this repository (None for public imports)."""
    if target.source is RepoSource.PUBLIC_URL:
        return None
    app = get_runtime().github_app
    if app is None or target.installation_id is None:
        raise CloneError(
            "github_not_configured", "The GitHub App isn't available to read this repository."
        )
    try:
        token = await app.installation_token(
            target.installation_id,
            repository_ids=[target.github_repo_id],
            permissions=CLONE_PERMISSIONS,
        )
    except GitHubError as exc:
        if exc.status in (401, 403, 404, 422):  # uninstalled, or the repo is no longer covered
            raise AccessLostError("github_access_lost", ACCESS_LOST_MESSAGE) from None
        raise
    return token.token


async def _repository_exists(target: Target, credential: Credential) -> bool:
    """After a 404 for the branch: is it the branch that's missing, or the whole repository?"""
    try:
        await get_runtime().github_client.get_json(f"/repos/{target.full_name}", credential)
    except GitHubError as exc:
        if exc.status in (403, 404):
            return False
        raise
    return True


async def _resolve(target: Target, token: str | None) -> str:
    """The commit the branch points at now."""
    credential = (
        Credential(f"Bearer {token}", f"installation-repo:{target.github_repo_id}")
        if token
        else Credential.anonymous()
    )
    path = f"/repos/{target.full_name}/commits/{quote(target.ref, safe='')}"
    try:
        body = as_object(await get_runtime().github_client.get_json(path, credential))
    except RateLimited as exc:
        wait = (exc.reset_at - _now()).total_seconds()
        raise RetryableError("GitHub rate limit", retry_after=max(1.0, wait)) from exc
    except GitHubError as exc:
        if exc.status in (401, 403):
            raise AccessLostError("github_access_lost", ACCESS_LOST_MESSAGE) from None
        if exc.status == 404 and not await _repository_exists(target, credential):
            public = target.source is RepoSource.PUBLIC_URL  # gone private, or deleted
            raise AccessLostError(
                "repo_not_public" if public else "github_access_lost",
                NOT_PUBLIC_MESSAGE if public else ACCESS_LOST_MESSAGE,
            ) from None
        if exc.status in (404, 409, 422):
            raise CloneError(
                "ref_not_found", f"The branch {target.ref!r} wasn't found on GitHub."
            ) from None
        raise
    sha = (body or {}).get("sha")
    if not isinstance(sha, str):
        raise CloneError("ref_not_found", f"GitHub didn't return a commit for {target.ref!r}.")
    return sha


async def _snapshot(task: SenseiTask, target: Target, job_id: uuid.UUID, sha: str) -> bool:
    """Attach the job to the snapshot of ``sha``; True if it's already archived (reuse)."""
    storage = await get_runtime().storage()
    async with task.org_session(target.org_id) as db:
        snapshot = await db.scalar(
            select(Snapshot)
            .where(Snapshot.repo_id == target.repo_id, Snapshot.commit_sha == sha)
            .with_for_update()
        )
        if snapshot is None:
            snapshot = Snapshot(
                org_id=target.org_id, repo_id=target.repo_id, ref=target.ref, commit_sha=sha
            )
            db.add(snapshot)
            await db.flush()
        job = await db.get(IndexJob, job_id)
        if job is None or job.status is not JobStatus.RUNNING:
            raise JobGoneError("the job ended meanwhile")
        job.snapshot_id = snapshot.id
        key = snapshot_key(target.org_id, target.repo_id, sha)
        archived = await storage.size(Bucket.SNAPSHOTS, key) is not None
        if snapshot.status is SnapshotStatus.READY and archived:
            return True
        snapshot.status = SnapshotStatus.PENDING
        return False


async def _upload(target: Target, sha: str, archive_path: Any) -> None:
    storage = await get_runtime().storage()
    key = snapshot_key(target.org_id, target.repo_id, sha)
    await storage.put_file(Bucket.SNAPSHOTS, key, archive_path, content_type="application/zstd")


async def _finish(
    task: SenseiTask,
    target: Target,
    job_id: uuid.UUID,
    sha: str,
    details: dict[str, Any],
    *,
    file_count: int,
    size: int,
) -> None:
    async with job_scope(task, target.org_id, job_id) as scope:
        scope.require(Stage.CLONE)
        snapshot = await scope.db.scalar(
            select(Snapshot).where(Snapshot.repo_id == target.repo_id, Snapshot.commit_sha == sha)
        )
        if snapshot is not None and snapshot.status is not SnapshotStatus.READY:
            snapshot.status = SnapshotStatus.READY
            snapshot.file_count = file_count
            snapshot.bytes = size
            snapshot.ready_at = _now()
        machine = scope.machine
        machine.update_progress(Stage.CLONE, STEPS, STEPS)
        machine.note(Stage.CLONE, commit_sha=sha, **details)
        machine.log(f"Checked out {sha[:12]}: {details['files']:,} files.")
        if details.get("submodules_skipped"):
            count = details["submodules_skipped"]
            machine.log(
                f"{count} submodule(s) not fetched: their code isn't indexed.", level="warn"
            )
        machine.complete(Stage.CLONE)


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.CLONE_SNAPSHOT.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def clone_snapshot(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> str | None:
    """Clone the job's repository at its branch's current commit; returns the commit."""
    runtime = get_runtime()
    settings = runtime.settings
    job = uuid.UUID(job_id)
    target = runtime.run(_begin(self, org_id, job))
    if target is None:
        return None
    started = time.monotonic()
    ingest = settings.ingest
    git = GitRunner(ingest)
    try:
        if target.size_kb * 1024 > ingest.max_checkout_bytes * HISTORY_SIZE_FACTOR:
            raise CloneError(
                "repo_too_large",
                f"The repository is {target.size_kb / 1024**2:.1f} GB on GitHub, too large to "
                "index.",
            )
        token = runtime.run(_token(target))
        sha = runtime.run(_resolve(target, token))
        runtime.run(_step(self, org_id, job, 1))
        url = f"{settings.github.web_url.rstrip('/')}/{target.full_name}.git"
        name = str(target.repo_id)
        # This machine's own disk: make room before fetching (copies in use stay).
        evict(ingest.cache_dir, ingest.cache_max_bytes, keep={name})
        with repo_lock(ingest.cache_dir, name):
            checkout = git.checkout(
                CloneSource(url=url, token=token),
                sha,
                ingest.cache_dir / name,
                deadline=started + ingest.clone_timeout_seconds,
            )
            runtime.run(_step(self, org_id, job, 2))
            details: dict[str, Any] = {
                "files": checkout.files,
                "bytes": checkout.bytes,
                "submodules_skipped": checkout.submodules,
                "lfs": "pointers_only",
            }
            if not runtime.run(_snapshot(self, target, job, sha)):
                archives = ingest.cache_dir / ".archives"
                archives.mkdir(parents=True, exist_ok=True)
                path = archives / f"{job_id}.tar.zst"
                try:
                    info = write_archive(checkout.path, path)
                    runtime.run(_step(self, org_id, job, 3))
                    runtime.run(_upload(target, sha, path))
                    details["archive_bytes"] = info.bytes
                finally:
                    path.unlink(missing_ok=True)
        runtime.run(_step(self, org_id, job, 4))
        runtime.run(
            _finish(self, target, job, sha, details, file_count=checkout.files, size=checkout.bytes)
        )
    except CloneError as exc:
        log.warning("clone failed", code=exc.code)
        if isinstance(exc, AccessLostError):
            runtime.run(_access_lost(self, target))
        runtime.run(fail_job(self, org_id, job, exc.code, exc.message))
        return None
    except TransientGitError as exc:
        raise RetryableError("git server trouble") from exc
    finally:
        git.close()
    log.info("snapshot ready", commit_sha=sha, seconds=round(time.monotonic() - started, 1))
    return sha


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.EVICT_REPO_CACHE.value, system=True)
def evict_repo_cache(self: SenseiTask) -> int:
    """Keep the working-copy cache within its disk budget; copies being indexed stay."""
    runtime = get_runtime()

    async def _active() -> set[str]:
        async with self.system_session() as db:
            rows = await db.scalars(
                select(IndexJob.repo_id).where(IndexJob.status.in_(ACTIVE_JOB_STATUSES))
            )
            return {str(repo_id) for repo_id in rows}

    settings = runtime.settings.ingest
    removed = evict(settings.cache_dir, settings.cache_max_bytes, keep=runtime.run(_active()))
    if removed:
        log.info("repo cache evicted", copies=len(removed))
    return len(removed)
