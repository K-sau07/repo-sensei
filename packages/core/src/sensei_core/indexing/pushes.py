"""Pushes (5.1): a push to a branch a repository tracks indexes it again once the pushes stop.

A repository tracks its default branch and ``RepoSettings.tracked_branches``. Each push to one
is noted in Redis as the branch's pending push (its latest commit, the commit the first of the
run of pushes started from, and whether any was forced), and a timer is set for
:data:`DEBOUNCE_SECONDS` later, carrying the push's token. Only the latest push's timer starts a
run: an earlier one finds another token noted, and stops. While the repository has a run going
the timer waits for it, so a branch gets one follow-up run of everything pushed meanwhile, not
one per push.

The run is incremental from the last index. A forced push (history rewritten), or a last index
that can't be read, gets a full run instead, and says why in the log.
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol, cast

import orjson
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import WatchError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.indexing.events import JobOutbox
from sensei_core.models import IndexJob, JobKind, JobStatus, Repo, RepoStatus, Snapshot
from sensei_core.models.audit import ActorType
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.repos.service import RepoSettings

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 30  # a run starts this long after a branch's last push
WAIT_SECONDS = 30  # a timer held back by a run going tries again this often
PENDING_TTL_SECONDS = 24 * 3600
PENDING_PREFIX = "push:pending:"
HEADS = "refs/heads/"
PULLS = "pull/"  # a pull request's ref: its head, indexed as an overlay when the repository asks
TRIES = 5  # a note raced by another push's is tried again, this often


class Pending(BaseModel):
    """A branch's pushes not indexed yet, as one."""

    after: str  # the latest push's commit: what the run indexes
    before: str | None  # the commit the first of them started from
    forced: bool  # any of them rewrote history
    token: str  # the latest push's: only its timer starts the run


class PushTimers(Protocol):
    """Starts a branch's run later (the timer task, ``countdown`` seconds from now), and drops a
    closed pull request's overlay."""

    async def schedule(
        self, org_id: uuid.UUID, repo_id: uuid.UUID, branch: str, token: str, *, countdown: int
    ) -> None: ...

    async def drop(self, org_id: uuid.UUID, repo_id: uuid.UUID, ref: str) -> None: ...


def pull_ref(number: int) -> str:
    """A pull request's ref, as its overlay's snapshots and runs carry it: ``pull/12``."""
    return f"{PULLS}{number}"


def pending_key(repo_id: uuid.UUID, branch: str) -> str:
    return f"{PENDING_PREFIX}{repo_id}:{branch}"


def branch_of(ref: object) -> str | None:
    """The branch a push's ``ref`` names (``refs/heads/main`` → ``main``); None for a tag."""
    if not isinstance(ref, str) or not ref.startswith(HEADS) or ref == HEADS:
        return None
    return ref.removeprefix(HEADS)


def tracks(repo: Repo, branch: str) -> bool:
    """Whether a push to ``branch`` (or a pull request's head, ``pull/N``) indexes it again."""
    if branch == repo.default_branch:
        return True
    settings = RepoSettings.model_validate(repo.settings or {})
    if branch.startswith(PULLS) and branch.removeprefix(PULLS).isdigit():
        return settings.index_pull_requests
    return branch in settings.tracked_branches


async def forget(redis: Redis, repo_id: uuid.UUID, ref: str) -> None:
    """A ref's pending push, if any, dropped (a pull request closed: nothing to index)."""
    await redis.delete(pending_key(repo_id, ref))


async def note(
    redis: Redis,
    repo_id: uuid.UUID,
    branch: str,
    *,
    after: str,
    before: str | None,
    forced: bool,
    token: str,
) -> Pending:
    """Note a push as the branch's pending one, folded into what was pending: its commit and
    token replace theirs, the first's starting commit is kept, and forced stays forced."""
    key = pending_key(repo_id, branch)
    async with redis.pipeline(transaction=True) as pipe:
        for _ in range(TRIES):
            try:
                await pipe.watch(key)
                was = _read(cast("bytes | str | None", await pipe.get(key)))
                pending = Pending(
                    after=after,
                    before=was.before if was is not None else before,
                    forced=forced or (was is not None and was.forced),
                    token=token,
                )
                pipe.multi()
                pipe.set(key, pending.model_dump_json(), ex=PENDING_TTL_SECONDS)
                await pipe.execute()
                return pending
            except WatchError:
                continue
    msg = "the branch's pending push kept changing"
    raise RuntimeError(msg)


async def due(redis: Redis, repo_id: uuid.UUID, branch: str, token: str) -> Pending | None:
    """The branch's pending push, if ``token`` is still its latest (None: a later push's timer
    has it, or it was indexed)."""
    pending = _read(cast("bytes | str | None", await redis.get(pending_key(repo_id, branch))))
    return pending if pending is not None and pending.token == token else None


async def done(redis: Redis, repo_id: uuid.UUID, branch: str, token: str) -> None:
    """The pending push ``token`` was latest of has its run: it goes, unless a push came since
    (that one keeps its own timer)."""
    key = pending_key(repo_id, branch)
    async with redis.pipeline(transaction=True) as pipe:
        for _ in range(TRIES):
            try:
                await pipe.watch(key)
                pending = _read(cast("bytes | str | None", await pipe.get(key)))
                if pending is None or pending.token != token:
                    await pipe.unwatch()
                    return
                pipe.multi()
                pipe.delete(key)
                await pipe.execute()
                return
            except WatchError:
                continue


def _read(value: bytes | str | None) -> Pending | None:
    if value is None:
        return None
    try:
        return Pending.model_validate(orjson.loads(value))
    except ValueError:
        return None


async def running(db: AsyncSession, repo: Repo) -> bool:
    """Whether the repository has a run queued or going (it holds the next one back)."""
    found = await db.scalar(
        select(IndexJob.id).where(
            IndexJob.repo_id == repo.id,
            IndexJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
        )
    )
    return found is not None


async def kind_for(db: AsyncSession, repo: Repo, pending: Pending) -> tuple[JobKind, str | None]:
    """Incremental from the last index, or full, and why not incremental."""
    if pending.forced:
        return JobKind.FULL, "a forced push rewrote the branch's history"
    if repo.last_indexed_sha is None:
        return JobKind.FULL, "the repository has no index yet"
    last = await db.scalar(
        select(Snapshot).where(
            Snapshot.repo_id == repo.id, Snapshot.commit_sha == repo.last_indexed_sha
        )
    )
    if last is None or last.status is not SnapshotStatus.READY or last.merkle_root is None:
        return JobKind.FULL, "the last index can't be read to compare with"
    return JobKind.INCREMENTAL, None


class JobRunningError(Exception):
    """The repository has a run going: the push's run waits for it."""


async def start(
    db: AsyncSession, repo: Repo, branch: str, pending: Pending, outbox: JobOutbox
) -> IndexJob:
    """The run a branch's pending push asks for (the system's: no one asked for it), queued.
    Raises :class:`JobRunningError` if the repository has one queued or going."""
    kind, why = await kind_for(db, repo, pending)
    if why is not None:
        log.info(
            "push indexes in full",
            extra={"repo_id": str(repo.id), "branch": branch, "reason": why},
        )
    job = IndexJob(
        org_id=repo.org_id,
        repo_id=repo.id,
        kind=kind,
        ref=branch,
        target_sha=pending.after,
        status=JobStatus.QUEUED,
    )
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError as exc:  # the one-active-job index
        raise JobRunningError from exc
    await db.refresh(job)
    await audit(
        db,
        AuditAction.REPO_INDEX_STARTED,
        actor_user_id=None,
        actor_type=ActorType.SYSTEM,
        org_id=repo.org_id,
        target_type="repo",
        target_id=repo.id,
        meta={
            "job_id": str(job.id),
            "kind": kind.value,
            "trigger": "push",
            "branch": branch,
            "sha": pending.after,
            **({"full_because": why} if why else {}),
        },
    )
    outbox.queued(job)
    return job


def indexable(repo: Repo | None) -> bool:
    """Whether a pushed repository is one a run can index now."""
    return (
        repo is not None and repo.deleted_at is None and repo.status is not RepoStatus.ACCESS_LOST
    )
