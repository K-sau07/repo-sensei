"""Per-user rate limits: a token bucket in Redis, shared by every API process.

A bucket holds up to ``per_minute`` tokens and refills at ``per_minute`` a minute; a request
takes one, or is refused with how long until one is back. The bucket is read, refilled and taken
from in one optimistic transaction (``WATCH``: a concurrent change starts it over) on Redis's
clock, so concurrent requests and processes never overdraw it. Redis being unavailable lets
requests through (logged): a limit is not worth an outage of what it guards.
"""

from __future__ import annotations

import math
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from sensei_core.logging import get_logger

log = get_logger(__name__)

ATTEMPTS = 8  # transactions started over before letting a request through


class TokenBucket:
    """``per_minute`` requests a minute per key (see the module)."""

    def __init__(self, redis: Redis, *, name: str, per_minute: int) -> None:
        self._redis = redis
        self._prefix = f"ratelimit:{name}:"
        self._capacity = per_minute
        self._per_ms = per_minute / 60_000

    async def take(self, key: str) -> int | None:
        """None if the request may go ahead, else the seconds until it may (at least 1)."""
        try:
            for _ in range(ATTEMPTS):
                try:
                    return await self._take(self._prefix + key)
                except WatchError:
                    continue
        except RedisError as exc:
            log.warning("rate limit unavailable", error=type(exc).__name__)
            return None
        log.warning("rate limit contended", key=key)
        return None

    async def _take(self, key: str) -> int | None:
        async with self._redis.pipeline(transaction=True) as watched:
            # redis-py types its commands for both its clients; these are the async ones.
            pipe = cast("Any", watched)
            await pipe.watch(key)
            seconds, micros = await pipe.time()
            now = seconds * 1000 + micros // 1000
            tokens_raw, at_raw = await pipe.hmget(key, ["tokens", "at"])
            tokens = float(tokens_raw) if tokens_raw is not None else float(self._capacity)
            at = int(at_raw) if at_raw is not None else now
            tokens = min(float(self._capacity), tokens + max(0, now - at) * self._per_ms)
            wait_ms = 0.0
            if tokens >= 1:
                tokens -= 1
            else:
                wait_ms = (1 - tokens) / self._per_ms
            pipe.multi()
            pipe.hset(key, mapping={"tokens": repr(tokens), "at": now})
            pipe.pexpire(key, 61_000)
            await pipe.execute()
        return None if wait_ms <= 0 else max(1, math.ceil(wait_ms / 1000))
