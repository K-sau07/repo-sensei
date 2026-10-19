"""Per-process resources for tasks: one event loop, and the connections bound to it.

Celery calls tasks synchronously, while our database and Redis clients are async. Each worker
process keeps a single :class:`asyncio.Runner` (one loop for its lifetime) and runs task
coroutines on it, so connection pools are reused across tasks instead of rebuilt per task.
Everything is created lazily, after Celery forks its pool processes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

import redis
from redis.asyncio import Redis

from sensei_core.config import Settings, get_settings
from sensei_core.db import Database, make_database, make_system_database
from sensei_core.redis import make_redis
from sensei_core.telemetry import instrument_engine

T = TypeVar("T")


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._runner: asyncio.Runner | None = None
        self._database: Database | None = None
        self._system_database: Database | None = None
        self._redis: Redis | None = None
        self._sync_redis: redis.Redis | None = None

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner.run(coro)

    @property
    def database(self) -> Database:
        """App role: row-level security applies; set the org context before touching org data."""
        if self._database is None:
            self._database = make_database(self.settings.database)
            instrument_engine(self._database.engine)
        return self._database

    @property
    def system_database(self) -> Database:
        """System role (bypasses row-level security): only for tasks declared ``system=True``."""
        if self._system_database is None:
            self._system_database = make_system_database(self.settings.database)
            instrument_engine(self._system_database.engine)
        return self._system_database

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            self._redis = make_redis(self.settings.redis)
        return self._redis

    @property
    def sync_redis(self) -> redis.Redis:
        """For bookkeeping outside the event loop (idempotency locks)."""
        if self._sync_redis is None:
            self._sync_redis = redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                self.settings.redis.url.get_secret_value(),
                decode_responses=True,
                socket_connect_timeout=self.settings.redis.connect_timeout_seconds,
                socket_timeout=self.settings.redis.timeout_seconds,
            )
        return self._sync_redis

    def close(self) -> None:
        async def _close() -> None:
            if self._redis is not None:
                await self._redis.aclose()
            for database in (self._database, self._system_database):
                if database is not None:
                    await database.dispose()

        if self._runner is not None:
            try:
                self._runner.run(_close())
            finally:
                self._runner.close()
        if self._sync_redis is not None:
            self._sync_redis.close()
        self._runner = None
        self._database = self._system_database = None
        self._redis = None
        self._sync_redis = None


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime  # noqa: PLW0603 - one runtime per process
    if _runtime is None:
        _runtime = Runtime(get_settings())
    return _runtime


def set_runtime(runtime: Runtime | None) -> None:
    """Install a runtime (tests), or drop the current one after closing it."""
    global _runtime  # noqa: PLW0603
    if _runtime is not None and _runtime is not runtime:
        _runtime.close()
    _runtime = runtime
