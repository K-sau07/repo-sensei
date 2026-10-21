"""Index jobs: history, reindex, cancel, and live progress as server-sent events."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, BackgroundTasks, Path, Request, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse, ServerSentEvent

from sensei_api import streams
from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoDep, repo_access
from sensei_api.routers.auth import GitHubOAuthDep
from sensei_core.auth.sessions import ActiveSession
from sensei_core.db import Database
from sensei_core.db.tenancy import set_org_context, set_user_context
from sensei_core.github.client import GitHubError
from sensei_core.github.oauth import GitHubOAuth, OAuthError
from sensei_core.indexing.events import TERMINAL, EventType, JobOutbox, job_stream, org_stream
from sensei_core.indexing.features import Feature, unlocked
from sensei_core.indexing.jobs import IndexJobService
from sensei_core.jobs import RepoJobs, chain_key
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    IndexJob,
    JobKind,
    JobStatus,
    Membership,
    Org,
    Repo,
    Stage,
    User,
    UserSession,
)

router = APIRouter(prefix="/orgs/{org}", tags=["indexing"])
JobId = Annotated[uuid.UUID, Path(description="Index job id")]
ACCESS_RECHECK_SECONDS = 300.0  # how long an org stream trusts its answer about one repository
MEMBERSHIP_RECHECK_SECONDS = 60.0  # how often an org stream checks its session and membership


class IndexJobOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID | None
    kind: JobKind
    status: JobStatus
    stage: Stage | None
    stages: dict[str, Any] = Field(
        description="Per-stage progress: status, done/total, rate, timestamps, details, error."
    )
    progress: float = Field(description="0 to 1, weighted by how long stages usually take.")
    eta_s: int | None = Field(description="Seconds left, when there is a rate to go by.")
    error: dict[str, Any] | None
    features: list[Feature] = Field(description="What the repository can do after this run.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def build(cls, job: IndexJob) -> IndexJobOut:
        stages = job.stages or {}
        done = [s for s in Stage if stages.get(s.value, {}).get("status") == "done"]
        return cls(
            id=job.id,
            repo_id=job.repo_id,
            snapshot_id=job.snapshot_id,
            kind=job.kind,
            status=job.status,
            stage=job.stage,
            stages=stages,
            progress=job.progress,
            eta_s=job.eta_s,
            error=job.error,
            features=unlocked(done),
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


def _jobs(request: Request) -> RepoJobs:
    jobs: RepoJobs = request.app.state.repo_jobs
    return jobs


def _service(request: Request) -> IndexJobService:
    service: IndexJobService | None = getattr(request.app.state, "index_jobs", None)
    return service or IndexJobService()


async def dispatch_index(
    redis: Redis,
    jobs: RepoJobs,
    outbox: JobOutbox,
    *,
    org_id: uuid.UUID,
    repo_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """After the commit: announce the queued job, then send it to the workers."""
    await outbox.publish(redis)
    await jobs.index(org_id, repo_id, job_id)


@router.get("/repos/{repo}/index-jobs", response_model=list[IndexJobOut])
async def list_index_jobs(request: Request, repo: RepoDep, db: DbSession) -> list[IndexJobOut]:
    """The repository's index runs, newest first (at most 50)."""
    return [IndexJobOut.build(job) for job in await _service(request).history(db, repo)]


@router.get("/repos/{repo}/index-jobs/{job}", response_model=IndexJobOut)
async def get_index_job(request: Request, repo: RepoDep, job: JobId, db: DbSession) -> IndexJobOut:
    return IndexJobOut.build(await _service(request).get(db, repo, job))


@router.post(
    "/repos/{repo}/reindex", response_model=IndexJobOut, status_code=status.HTTP_202_ACCEPTED
)
async def reindex(
    *,
    request: Request,
    repo: RepoDep,
    access: OrgAccessDep,
    db: DbSession,
    redis: RedisClient,
    background: BackgroundTasks,
) -> IndexJobOut:
    """Index the repository again at its branch's current commit (admins). 409
    ``index_job_running`` while a run is queued or running."""
    outbox = JobOutbox()
    job = await _service(request).start(db, access, repo, outbox)
    background.add_task(
        dispatch_index,
        redis,
        _jobs(request),
        outbox,
        org_id=repo.org_id,
        repo_id=repo.id,
        job_id=job.id,
    )
    return IndexJobOut.build(job)


@router.post("/repos/{repo}/index-jobs/{job}/cancel", response_model=IndexJobOut)
async def cancel_index_job(
    *,
    request: Request,
    repo: RepoDep,
    job: JobId,
    access: OrgAccessDep,
    db: DbSession,
    redis: RedisClient,
    background: BackgroundTasks,
) -> IndexJobOut:
    """Cancel a queued or running run (admins): its stages stop at their next step."""
    outbox = JobOutbox()
    cancelled = await _service(request).cancel(db, access, repo, job, outbox)

    async def after_commit() -> None:
        await outbox.publish(redis)
        pending = cast(
            "Awaitable[list[str]]",
            redis.lrange(chain_key(job), 0, -1),  # pyright: ignore[reportUnknownMemberType]
        )
        task_ids = await pending
        await _jobs(request).revoke(list(task_ids))

    background.add_task(after_commit)
    return IndexJobOut.build(cancelled)


def _final_event(job: IndexJob) -> dict[str, Any]:
    """A stand-in for the last event of an ended run whose stream doesn't have it (expired, or
    the event was never published)."""
    kind = {
        JobStatus.SUCCEEDED: EventType.DONE,
        JobStatus.FAILED: EventType.FAILED,
        JobStatus.CANCELLED: EventType.CANCELLED,
    }[job.status]
    payload: dict[str, Any] = {
        "type": kind.value,
        "job_id": str(job.id),
        "repo_id": str(job.repo_id),
        "stage": job.stage.value if job.stage else None,
        "ts": (job.finished_at or job.created_at).isoformat(),
        "id": None,
    }
    if job.error:
        payload.update(code=job.error.get("code"), message=job.error.get("message"))
    return payload


@router.get(
    "/repos/{repo}/index-jobs/{job}/events",
    response_class=EventSourceResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def index_job_events(
    request: Request, repo: RepoDep, job: JobId, db: DbSession, redis: RedisClient
) -> EventSourceResponse:
    """The run's progress as server-sent events (types: queued, stage_started, progress,
    stage_done, unlock, log, done, failed, cancelled). Replays from ``Last-Event-ID``; closes
    after the final event."""
    found = await _service(request).get(db, repo, job)
    cursor = streams.last_event_id(request)
    active = found.status in ACTIVE_JOB_STATUSES
    key = job_stream(job)
    stop_on = frozenset(t.value for t in TERMINAL)
    org_id = repo.org_id
    latest: list[IndexJob] = [found]

    async def still_active() -> bool:
        async with _short_session(request) as session:
            await set_org_context(session, org_id)
            current = await session.get(IndexJob, job)
            if current is None:
                return False
            latest[0] = current
            return current.status in ACTIVE_JOB_STATUSES

    async def events() -> AsyncGenerator[ServerSentEvent]:
        ended = False
        async for item in streams.follow(
            request, redis, key, cursor, stop_on=stop_on, follow_new=active, still=still_active
        ):
            ended = ended or item.event in stop_on
            yield item
        if ended or await request.is_disconnected():
            return
        if latest[0].status in ACTIVE_JOB_STATUSES:
            await still_active()  # the last word from the database
        if latest[0].status not in ACTIVE_JOB_STATUSES:
            # The run is over but its stream doesn't say so: what the database knows.
            yield streams.event(_final_event(latest[0]))

    return streams.response(events())


@router.get(
    "/events",
    response_class=EventSourceResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def org_events(
    request: Request, access: OrgAccessDep, redis: RedisClient, oauth: GitHubOAuthDep
) -> EventSourceResponse:
    """The org's index runs starting and ending, as server-sent events (for status dots and the
    inbox), limited to repositories the signed-in user can read. Closes only when the session
    ends or the user leaves the org (checked every minute); the browser's reconnect then gets
    the error."""
    org_id, user_id = access.org.id, access.user.id
    active: ActiveSession = request.state.active_session
    session_id = active.session.id
    key = org_stream(org_id)
    cursor = (
        streams.last_event_id(request)
        if _resuming(request)
        else await streams.latest_id(redis, key)
    )
    known: dict[str, tuple[bool, float]] = {}
    allowed = True

    async def still_allowed() -> bool:
        nonlocal allowed
        allowed = await _still_member(request, org_id, user_id, session_id)
        return allowed

    async def keep(payload: dict[str, Any]) -> bool:
        if not allowed:
            return False  # nothing more, not even what was already in the stream
        repo_id = str(payload.get("repo_id"))
        cached = known.get(repo_id)
        if cached is None or time.monotonic() - cached[1] > ACCESS_RECHECK_SECONDS:
            readable = await _can_read(request, oauth, org_id, user_id, repo_id)
            known[repo_id] = cached = (readable, time.monotonic())
        return cached[0]

    return streams.response(
        streams.follow(
            request,
            redis,
            key,
            cursor,
            keep=keep,
            still=still_allowed,
            recheck_seconds=MEMBERSHIP_RECHECK_SECONDS,
        )
    )


async def _still_member(
    request: Request, org_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    """Whether the stream's session is still live and its user still in the org. Read-only: a
    stream mustn't keep a session alive the way a request does."""
    async with _short_session(request) as db:
        await set_user_context(db, user_id)
        await set_org_context(db, org_id)
        live = await db.scalar(
            select(UserSession.id).where(
                UserSession.id == session_id,
                UserSession.revoked_at.is_(None),
                UserSession.idle_expires_at > func.now(),
                UserSession.absolute_expires_at > func.now(),
            )
        )
        member = await db.scalar(
            select(Membership.user_id)
            .join(Org, Org.id == Membership.org_id)
            .where(
                Membership.org_id == org_id,
                Membership.user_id == user_id,
                Org.deleted_at.is_(None),
            )
        )
        return live is not None and member is not None


def _resuming(request: Request) -> bool:
    return bool(request.headers.get("last-event-id") or request.query_params.get("last_event_id"))


async def _can_read(
    request: Request,
    oauth: GitHubOAuth | None,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    repo_id: str,
) -> bool:
    """Whether the user may read the repository, in a short session of its own."""
    try:
        target = uuid.UUID(repo_id)
    except ValueError:
        return False
    async with _short_session(request) as db:
        await set_org_context(db, org_id)
        repo = await db.get(Repo, target)
        user = await db.get(User, user_id)
        if repo is None or user is None or repo.deleted_at is not None:
            return False
        try:
            return await repo_access(request, db, oauth).can_read(user, repo)
        except (OAuthError, GitHubError):
            return False


def _short_session(request: Request) -> AbstractAsyncContextManager[AsyncSession]:
    """A session of its own (streams outlive the request's); tests may swap the factory."""
    factory = getattr(request.app.state, "stream_sessions", None)
    if factory is not None:
        return cast("Callable[[], AbstractAsyncContextManager[AsyncSession]]", factory)()
    database: Database = request.app.state.database
    return database.session()
