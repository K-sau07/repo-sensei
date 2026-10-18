"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sensei_core.config import DatabaseSettings


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


def make_database(settings: DatabaseSettings, *, url: str | None = None) -> Database:
    engine = create_async_engine(
        url or settings.url.get_secret_value(),
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=True,
        echo=settings.echo,
    )
    return Database(
        engine=engine,
        sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
    )
