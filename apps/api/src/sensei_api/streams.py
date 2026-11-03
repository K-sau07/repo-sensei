"""Server-sent events from Redis Streams: replay from ``Last-Event-ID``, then follow.

Each SSE event's ``id`` is the stream entry id, so a browser that reconnects (``EventSource``
does by itself) sends ``Last-Event-ID`` and continues exactly after what it saw. A comment line
goes out every 15 seconds so proxies keep the connection open. Responses are never cached or
transformed (``Cache-Control: no-cache, no-transform``) nor buffered by nginx-style proxies
(``X-Accel-Buffering: no``). The generator holds no database connection: whatever it needs from
Postgres is read before streaming starts, or in short sessions of its own.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

import orjson
from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse, ServerSentEvent

from sensei_core.db import Database
from sensei_core.db.tenancy import set_org_context, set_user_context
from sensei_core.errors import InvalidError
from sensei_core.indexing.events import parse_entry
from sensei_core.models import Membership, Org, UserSession

HEARTBEAT_SECONDS = 15.0
# Each read blocks for at most this long: below the Redis client's socket timeout, so a quiet
# stream is a timeout-free loop (heartbeats go out from sse-starlette meanwhile).
READ_BLOCK_MS = 4_000
READ_COUNT = 200
HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
_ENTRY_ID = re.compile(r"\d{1,20}(?:-\d{1,20})?")  # "0" is the start

RECHECK_SECONDS = 30.0  # how often a following stream asks whether it should go on

Filter = Callable[[dict[str, Any]], Awaitable[bool]]
Check = Callable[[], Awaitable[bool]]


def last_event_id(request: Request) -> str:
    """Where to resume: the ``Last-Event-ID`` header (or ``?last_event_id=``), else the start."""
    value = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    if not value:
        return "0"
    if not _ENTRY_ID.fullmatch(value):
        raise InvalidError("Last-Event-ID isn't an event id.", code="invalid_last_event_id")
    return value


async def latest_id(redis: Redis, key: str) -> str:
    """The id of the stream's newest entry ("0-0" if empty): where "only what comes next"
    starts. (Reading from "$" again after each wait would drop events that arrive in between.)"""
    newest = await cast(
        "Awaitable[list[tuple[str, Any]]]",
        redis.xrevrange(key, count=1),  # pyright: ignore[reportUnknownMemberType]
    )
    return newest[0][0] if newest else "0-0"


def event(payload: dict[str, Any]) -> ServerSentEvent:
    return ServerSentEvent(
        data=orjson.dumps(payload).decode(), id=payload.get("id"), event=str(payload["type"])
    )


async def _read(redis: Redis, key: str, cursor: str, *, block: int | None) -> list[Any]:
    result = await cast(
        "Awaitable[list[Any] | None]",
        redis.xread({key: cursor}, count=READ_COUNT, block=block),  # pyright: ignore[reportUnknownMemberType]
    )
    return result or []


async def follow(
    request: Request,
    redis: Redis,
    key: str,
    cursor: str,
    *,
    stop_on: frozenset[str] = frozenset(),
    keep: Filter | None = None,
    follow_new: bool = True,
    still: Check | None = None,
    recheck_seconds: float | None = None,
) -> AsyncGenerator[ServerSentEvent]:
    """Events after ``cursor``; with ``follow_new``, keep waiting for more until one of
    ``stop_on`` (or the client goes away). ``keep`` drops events the viewer mustn't see.

    ``still`` is asked every ``recheck_seconds`` (default ``RECHECK_SECONDS``) while following:
    once it says no, what the stream already has is sent (through ``keep``) and it ends. That
    covers what Redis alone can't tell, like a final event that was never published or a viewer
    who lost access."""
    every = RECHECK_SECONDS if recheck_seconds is None else recheck_seconds
    checked = time.monotonic()
    while True:
        if follow_new and still is not None and time.monotonic() - checked >= every:
            checked = time.monotonic()
            if not await still():
                follow_new = False  # drain what's there, then stop
        batches = await _read(redis, key, cursor, block=READ_BLOCK_MS if follow_new else None)
        for _stream, entries in batches:
            for entry_id, fields in entries:
                cursor = entry_id
                payload = parse_entry(entry_id, cast("dict[str, str]", fields))
                if keep is not None and not await keep(payload):
                    continue
                yield event(payload)
                if payload["type"] in stop_on:
                    return
        if not batches and not follow_new:
            return
        if await request.is_disconnected():
            return


def response(events: AsyncGenerator[ServerSentEvent]) -> EventSourceResponse:
    return EventSourceResponse(events, ping=HEARTBEAT_SECONDS, headers=HEADERS)


def short_session(request: Request) -> AbstractAsyncContextManager[AsyncSession]:
    """A session of its own (streams outlive the request's); tests may swap the factory."""
    factory = getattr(request.app.state, "stream_sessions", None)
    if factory is not None:
        return cast("Callable[[], AbstractAsyncContextManager[AsyncSession]]", factory)()
    database: Database = request.app.state.database
    return database.session()


async def still_member(
    request: Request, org_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    """Whether the stream's session is still live and its user still in the org. Read-only: a
    stream mustn't keep a session alive the way a request does."""
    async with short_session(request) as db:
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
