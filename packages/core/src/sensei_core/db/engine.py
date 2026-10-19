"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sensei_core.config import DatabaseSettings


def rowcount(result: Result[Any]) -> int:
    """Rows affected by an UPDATE/DELETE executed through ``AsyncSession.execute``."""
    return cast("CursorResult[Any]", result).rowcount


@dataclass(frozen=True, slots=True)
class Database:
    """An engine plus its session factory. One per process (API, worker)."""

    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        """A session in a transaction: commits on success, rolls back on error."""
        async with self.sessionmaker() as session, session.begin():
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()


def make_system_database(settings: DatabaseSettings) -> Database:
    """A database handle for the BYPASSRLS ``sensei_system`` role.

    Only for explicitly named maintenance tasks (cross-org purges, staff tooling). Never use it to
    serve a user request.
    """
    return make_database(settings, url=settings.system_url.get_secret_value())


def make_database(settings: DatabaseSettings, *, url: str | None = None) -> Database:
    engine = create_async_engine(
        url or settings.url.get_secret_value(),
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=True,
        pool_timeout=settings.connect_timeout_seconds,
        connect_args={"timeout": settings.connect_timeout_seconds},
        echo=settings.echo,
    )
    return Database(
        engine=engine,
        sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
    )
