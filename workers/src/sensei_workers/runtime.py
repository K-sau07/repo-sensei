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

import httpx
import redis
from redis.asyncio import Redis

from sensei_core.config import Settings, get_settings
from sensei_core.db import Database, make_database, make_system_database
from sensei_core.github.app import GitHubApp
from sensei_core.github.client import GitHubClient
from sensei_core.github.etags import RedisETagCache
from sensei_core.redis import make_redis
from sensei_core.security.crypto import TokenCipher
from sensei_core.storage import ObjectStorage
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
        self._http: httpx.AsyncClient | None = None
        self._github_client: GitHubClient | None = None
        self._github_app: GitHubApp | None = None
        self._storage: ObjectStorage | None = None

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        if self._runner is None:
            self._runner = asyncio.Runner()
        try:
            return self._runner.run(coro)
        except BaseException:
            # An exception raised from a signal handler (Celery's soft time limit) can leave the
            # loop while the coroutine is still waiting on I/O. Left alone, it would resume inside
            # the next task's run, still holding its session and locks: cancel it now.
            self._cancel_leftovers()
            raise

    def _cancel_leftovers(self) -> None:
        if self._runner is None:
            return
        loop = self._runner.get_loop()
        if loop.is_closed() or loop.is_running():
            return
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

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

    @property
    def github_client(self) -> GitHubClient:
        """GitHub's REST API with the shared (encrypted) ETag cache; any credential."""
        if self._github_client is None:
            self._http = httpx.AsyncClient(timeout=self.settings.github.timeout_seconds)
            self._github_client = GitHubClient(
                self.settings.github, self._http, etags=RedisETagCache(self.redis, self._cipher)
            )
        return self._github_client

    @property
    def github_app(self) -> GitHubApp | None:
        """Acting as the GitHub App (None when this deployment has none configured)."""
        if self._github_app is None and self.settings.github.app_configured:
            self._github_app = GitHubApp(
                self.settings.github, self.github_client, self.redis, self._cipher
            )
        return self._github_app

    @property
    def _cipher(self) -> TokenCipher:
        return TokenCipher.from_settings(self.settings.auth)

    async def storage(self) -> ObjectStorage:
        """Object storage, started on first use (on this runtime's event loop)."""
        if self._storage is None:
            storage = ObjectStorage(self.settings.s3)
            await storage.start()
            self._storage = storage
        return self._storage

    def close(self) -> None:
        async def _close() -> None:
            if self._storage is not None:
                await self._storage.close()
            if self._http is not None:
                await self._http.aclose()
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
        self._http = None
        self._github_client = None
        self._github_app = None
        self._storage = None


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
