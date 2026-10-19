"""Async Redis client factory."""

from __future__ import annotations

from redis.asyncio import Redis

from sensei_core.config import RedisSettings


def make_redis(settings: RedisSettings) -> Redis:
    return Redis.from_url(  # pyright: ignore[reportUnknownMemberType] - redis-py stub gap
        settings.url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=settings.connect_timeout_seconds,
        socket_timeout=settings.timeout_seconds,
        health_check_interval=30,
    )


async def ping_redis(client: Redis) -> None:
    pong = await client.ping()  # pyright: ignore[reportUnknownMemberType]
    if pong is not True:
        msg = f"unexpected redis ping reply: {pong!r}"
        raise ConnectionError(msg)
