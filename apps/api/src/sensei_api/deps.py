"""Request-scoped FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db import Database
from sensei_core.limits import require_writable


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


async def get_db(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncGenerator[AsyncSession]:
    """One transaction per request: committed when the handler succeeds, rolled back on error.

    Used with ``scope="function"`` (see :data:`DbSession`) so the commit happens *before* the
    response is sent: if the commit fails, the client gets an error instead of a false success.
    """
    async with database.session() as session:
        yield session


def get_redis(request: Request) -> Redis:
    client: Redis = request.app.state.redis
    return client


DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]
RedisClient = Annotated[Redis, Depends(get_redis)]


async def writable(redis: RedisClient) -> None:
    """Refuses (503 ``read_only``) while every org's spend today is past the global cap (10.1):
    on what starts something that costs (an ask, a diagram, a fix or change, an index run)."""
    await require_writable(redis)


Writable = Depends(writable)
