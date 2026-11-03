"""Index jobs: history, reindex, cancel, and live progress as server-sent events."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Awaitable
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, BackgroundTasks, Path, Request, status
from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis
from sse_starlette import EventSourceResponse, ServerSentEvent

from sensei_api import stream_access, streams
from sensei_api.auth import RequiredSession
from sensei_api.deps import DbSession, RedisClient, Writable
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoDep
from sensei_api.routers.auth import GitHubOAuthDep
from sensei_core.config import LimitsSettings
from sensei_core.db.tenancy import set_org_context
from sensei_core.indexing.events import TERMINAL, EventType, JobOutbox, job_stream, org_stream
from sensei_core.indexing.features import Feature, unlocked
from sensei_core.indexing.jobs import IndexJobService
from sensei_core.jobs import RepoJobs, chain_key
from sensei_core.limits import check_quota, count_quota
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    IndexJob,
    JobKind,
    JobStatus,
    Stage,
)

router = APIRouter(prefix="/orgs/{org}", tags=["indexing"])
JobId = Annotated[uuid.UUID, Path(description="Index job id")]
MEMBERSHIP_RECHECK_SECONDS = 60.0  # how often an org stream checks its session and membership


class PartialOut(BaseModel):
    """What a stage done with work left for the next index cut short."""

    files: int | None = Field(
        default=None, description="Files whose summary retries were cut short: still none."
    )
    directories: bool | None = Field(
        default=None, description="Whether the directory summaries were cut short."
    )


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
    partial: dict[str, PartialOut] = Field(
        description="Stages done with work left for the next index, by stage: what was cut"
        " short. A feature they unlock works on what was written."
    )
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
            partial=_partial(stages),
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


def _partial(stages: dict[str, Any]) -> dict[str, PartialOut]:
    """What each stage done with work left for the next index cut short (its details'); a
    malformed entry is left out."""
    found: dict[str, PartialOut] = {}
    for name, raw in stages.items():
        entry = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
        details = entry.get("details")
        partial = (
            cast("dict[str, Any]", details).get("partial") if isinstance(details, dict) else None
        )
        if entry.get("status") != "done" or not isinstance(partial, dict) or not partial:
            continue
        try:
            found[name] = PartialOut.model_validate(partial)
        except ValidationError:
            continue
    return found


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
    "/repos/{repo}/reindex",
    response_model=IndexJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Writable],
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
    ``index_job_running`` while a run is queued or running; 429 ``quota_exceeded`` past the
    person's runs for the day."""
    limits: LimitsSettings = request.app.state.settings.limits
    await check_quota(redis, "index_runs", access.user.id, limits.index_runs_per_day,
                      what="index runs")  # fmt: skip
    outbox = JobOutbox()
    job = await _service(request).start(db, access, repo, outbox)
    await count_quota(redis, "index_runs", access.user.id)  # started (not refused)
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
    stage_done, unlock, log, constellation, done, failed, cancelled). Replays from
    ``Last-Event-ID``; closes after the final event."""
    found = await _service(request).get(db, repo, job)
    cursor = streams.last_event_id(request)
    active = found.status in ACTIVE_JOB_STATUSES
    key = job_stream(job)
    stop_on = frozenset(t.value for t in TERMINAL)
    org_id = repo.org_id
    latest: list[IndexJob] = [found]

    async def still_active() -> bool:
        async with streams.short_session(request) as session:
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
    request: Request,
    access: OrgAccessDep,
    active: RequiredSession,
    redis: RedisClient,
    oauth: GitHubOAuthDep,
) -> EventSourceResponse:
    """The org's index runs starting and ending, as server-sent events (for status dots and the
    inbox), limited to repositories the signed-in user can read. Closes only when the session
    ends or the user leaves the org (checked every minute); the browser's reconnect then gets
    the error."""
    # A browser's stream, kept open while its session lives: API keys and tokens can't.
    org_id, user_id = access.org.id, access.user.id
    session_id = active.session.id
    key = org_stream(org_id)
    cursor = (
        streams.last_event_id(request)
        if _resuming(request)
        else await streams.latest_id(redis, key)
    )
    readable = stream_access.readable_repos(request, oauth, org_id, user_id)
    allowed = True

    async def still_allowed() -> bool:
        nonlocal allowed
        allowed = await streams.still_member(request, org_id, user_id, session_id)
        return allowed

    async def keep(payload: dict[str, Any]) -> bool:
        if not allowed:
            return False  # nothing more, not even what was already in the stream
        return await readable(str(payload.get("repo_id")))

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


def _resuming(request: Request) -> bool:
    return bool(request.headers.get("last-event-id") or request.query_params.get("last_event_id"))
