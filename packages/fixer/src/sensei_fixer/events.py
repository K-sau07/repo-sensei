"""A fix's events (8.4): what the run page streams. Each goes on the fix's Redis Stream
``fixes:{fix_id}`` (capped, kept for a week), replayed from any point by the SSE endpoint;
status changes people act on (waiting for approval, done, stopped) also go on the org's stream
``org:{org_id}:events`` for the inbox. They are collected while the run's row is locked and
published after the transaction commits (:class:`FixOutbox`), so nobody hears about a change
that was rolled back.

Types: ``queued``, ``stage_started``, ``step`` (``name``, ``summary``), ``log`` (``level``,
``message``), ``stage_done`` (``finished``), ``awaiting_approval``, ``approved``,
``replanning``, ``done`` (``pr_url``), and the stops: ``not_reproduced``, ``blocked``,
``failed``, ``rejected``, ``cancelled`` (``reason``).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from typing import Any, cast

import orjson
from redis.asyncio import Redis

STREAM_MAXLEN = 5_000
ORG_STREAM_MAXLEN = 1_000
STREAM_TTL_SECONDS = 7 * 24 * 3600
FINAL = frozenset({"done", "not_reproduced", "blocked", "failed", "rejected", "cancelled"})
ORG_TYPES = FINAL | {"awaiting_approval"}


def fix_stream(fix_id: uuid.UUID) -> str:
    return f"fixes:{fix_id}"


def org_stream(org_id: uuid.UUID) -> str:
    return f"org:{org_id}:events"


@dataclass
class FixOutbox:
    org_id: uuid.UUID
    fix_id: uuid.UUID
    repo_id: uuid.UUID
    pending: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def add(self, events: Iterable[dict[str, Any]]) -> None:
        self.pending.extend(events)

    async def publish(self, redis: Redis) -> None:
        """Send what was collected (after the commit). Best effort: the database is the truth,
        and the stream endpoint falls back to it."""
        events, self.pending = self.pending, []
        if not events:
            return
        key = fix_stream(self.fix_id)
        try:
            for event in events:
                payload = {**event, "repo_id": str(self.repo_id)}
                await _xadd(redis, key, payload, STREAM_MAXLEN)
                if event["type"] in ORG_TYPES:
                    await _xadd(redis, org_stream(self.org_id), {**payload, "kind": "fix"},
                                ORG_STREAM_MAXLEN)  # fmt: skip
            await cast("Awaitable[Any]", redis.expire(key, STREAM_TTL_SECONDS))
        except Exception:  # Redis down: the run goes on, and the page reads the row
            return


async def _xadd(redis: Redis, key: str, payload: dict[str, Any], maxlen: int) -> None:
    fields = {"data": orjson.dumps(payload)}
    adding = redis.xadd(key, fields, maxlen=maxlen, approximate=True)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    await cast("Awaitable[Any]", adding)
