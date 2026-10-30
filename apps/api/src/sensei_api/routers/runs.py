"""Agent runs (4.8): a run with its steps and the data its waterfall draws, a repository's runs,
and a live run's steps as they come (server-sent events). A run is seen by whoever in its org
may read its repository."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Awaitable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, cast

import orjson
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse, ServerSentEvent

from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoAccessDep, RepoDep, checked
from sensei_api.runs import RUN_PREFIX, step_data
from sensei_api.streams import HEADERS, HEARTBEAT_SECONDS, READ_BLOCK_MS, last_event_id
from sensei_core.errors import NotFoundError
from sensei_core.models import AgentRun, AgentStep, Repo, User
from sensei_core.orgs.service import OrgAccess
from sensei_core.repos.access import RepoAccess

router = APIRouter(prefix="/orgs/{org}", tags=["runs"])


class StepOut(BaseModel):
    idx: int
    kind: str
    name: str
    input: dict[str, Any]
    summary: str
    tokens: int
    ms: int
    offset_ms: int
    has_output: bool
    output: Any | None


class RunOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    kind: str
    question: str
    model: str
    mode: str
    status: str
    stopped: str | None
    user: str | None
    turns: int
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    cost_usd: Decimal
    latency_ms: int | None
    first_token_ms: int | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class RunDetail(RunOut):
    steps: list[StepOut]


class RunList(BaseModel):
    runs: list[RunOut]
    next_cursor: str | None


@router.get("/agent-runs/{run_id}", response_model=RunDetail)
async def run(
    *, access: OrgAccessDep, readers: RepoAccessDep, db: DbSession, run_id: uuid.UUID
) -> RunDetail:
    """A run with its steps in order: what each was, took and cost, where it starts in the run
    (the waterfall's bars)."""
    found = await _readable(db, access, readers, run_id)
    steps = list(
        await db.scalars(
            select(AgentStep).where(AgentStep.run_id == found.id).order_by(AgentStep.idx)
        )
    )
    login = await _login(db, found.user_id)
    return RunDetail(
        **_out(found, login).model_dump(),
        steps=[StepOut(**step_data(step, found), output=step.output) for step in steps],
    )


@router.get("/repos/{repo}/runs", response_model=RunList)
async def runs(
    *,
    repo: RepoDep,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[datetime | None, Query()] = None,
) -> RunList:
    """The repository's runs, the latest first (who asked what, its status, cost and latency)."""
    query = select(AgentRun, User.login).outerjoin(User, User.id == AgentRun.user_id)
    query = query.where(AgentRun.repo_id == repo.id)
    if cursor is not None:
        query = query.where(AgentRun.created_at < cursor)
    rows = list(
        await db.execute(
            query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit + 1)
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    after = rows[-1][0].created_at.isoformat() if more and rows else None
    return RunList(runs=[_out(found, login) for found, login in rows], next_cursor=after)


@router.get("/agent-runs/{run_id}/events", response_class=EventSourceResponse)
async def events(
    *,
    request: Request,
    access: OrgAccessDep,
    readers: RepoAccessDep,
    db: DbSession,
    redis: RedisClient,
    run_id: uuid.UUID,
) -> EventSourceResponse:
    """A run's steps as they are recorded (from ``Last-Event-ID``), until it finishes; a run
    that has finished, its steps, and its end."""
    found = await _readable(db, access, readers, run_id)
    key = f"{RUN_PREFIX}{found.id}"
    if found.finished_at is not None and not await redis.exists(key):
        steps = list(
            await db.scalars(
                select(AgentStep).where(AgentStep.run_id == found.id).order_by(AgentStep.idx)
            )
        )
        finished = {"type": "finished", "status": found.status}
        recorded = [step_data(step, found) for step in steps] + [finished]
        return EventSourceResponse(_replay(recorded), ping=HEARTBEAT_SECONDS, headers=HEADERS)
    stream = _follow(request, redis, key, last_event_id(request))
    return EventSourceResponse(stream, ping=HEARTBEAT_SECONDS, headers=HEADERS)


async def _replay(recorded: list[dict[str, Any]]) -> AsyncGenerator[ServerSentEvent]:
    for data in recorded:
        yield ServerSentEvent(data=orjson.dumps(data).decode(), event=str(data["type"]))


async def _follow(
    request: Request, redis: Redis, key: str, cursor: str
) -> AsyncGenerator[ServerSentEvent]:
    while True:
        batches = await cast(
            "Awaitable[list[Any] | None]",
            redis.xread({key: cursor}, count=200, block=READ_BLOCK_MS),  # pyright: ignore[reportUnknownMemberType]
        )
        for _stream, entries in batches or []:
            for entry_id, fields in entries:
                cursor = entry_id
                data = orjson.loads(fields.get(b"step", fields.get("step")))
                yield ServerSentEvent(
                    data=orjson.dumps(data).decode(), id=entry_id, event=str(data["type"])
                )
                if data["type"] == "finished":
                    return
        if await request.is_disconnected():
            return


async def _readable(
    db: AsyncSession, access: OrgAccess, readers: RepoAccess, run_id: uuid.UUID
) -> AgentRun:
    found = await db.get(AgentRun, run_id)
    repo = await db.get(Repo, found.repo_id) if found is not None else None
    if found is None or repo is None or not await checked(readers, access.user, [repo]):
        raise NotFoundError("Run not found.", code="run_not_found")
    await db.refresh(found)  # as stored: a run may have ended since it was loaded
    return found


async def _login(db: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    return await db.scalar(select(User.login).where(User.id == user_id))


def _out(found: AgentRun, login: str | None) -> RunOut:
    return RunOut(
        id=found.id,
        repo_id=found.repo_id,
        kind=found.kind,
        question=found.question,
        model=found.model,
        mode=found.mode,
        status=found.status,
        stopped=found.stopped,
        user=login,
        turns=found.turns,
        tokens_in=found.tokens_in,
        tokens_out=found.tokens_out,
        cache_read=found.cache_read,
        cache_write=found.cache_write,
        cost_usd=found.cost_usd,
        latency_ms=found.latency_ms,
        first_token_ms=found.first_token_ms,
        error=found.error,
        created_at=found.created_at,
        finished_at=found.finished_at,
    )
