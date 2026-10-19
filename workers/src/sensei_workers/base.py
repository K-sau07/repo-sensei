"""The base class every repo-sensei task uses.

Declared on the task decorator::

    @celery_app.task(bind=True, base=SenseiTask, name=..., requires=("org_id",),
                     idempotent_on=("job_id",), system=False)

* ``requires``: keyword arguments that must be present (refused before running otherwise).
* ``idempotent_on``: keyword arguments naming the unit of work. Delivery is at least once, so a
  task can run twice; a Redis lock owned by the Celery task id lets a redelivered run (same id)
  continue, makes a duplicate enqueue (another id) skip while the first is running, and marks the
  work done so later duplicates skip too.
* ``system``: the task may open system-role sessions (row-level security bypassed). Only for
  explicitly named cross-org work; other tasks get org-scoped sessions.

Failures are classified: network errors, 5xx, rate limits and :class:`RetryableError` are retried
with exponential backoff and jitter; anything else, or running out of retries, is final and is
written to ``job_failures``.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import httpx
import redis
import redis.exceptions
import sqlalchemy.exc
from billiard.einfo import ExceptionInfo
from celery import Task
from celery.exceptions import Retry
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import WorkerSettings
from sensei_core.context import bind_context
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.client import GitHubError, RateLimited
from sensei_core.logging import get_logger
from sensei_core.models import JobFailure
from sensei_core.redaction import redact
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)

MAX_TEXT = 20_000


class RetryableError(Exception):
    """A temporary failure: try again later (after ``retry_after`` seconds, if given)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


_TRANSIENT: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    ConnectionError,
    TimeoutError,
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
    sqlalchemy.exc.OperationalError,
    sqlalchemy.exc.InterfaceError,
)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (RetryableError, RateLimited, *_TRANSIENT)):
        return True
    if isinstance(exc, GitHubError):
        return exc.status >= 500 or exc.status == 429
    return isinstance(exc, sqlalchemy.exc.DBAPIError) and exc.connection_invalidated


def backoff(retries: int, settings: WorkerSettings) -> float:
    """Exponential with "equal jitter": half the delay is fixed, half random."""
    delay = min(settings.retry_backoff_max_seconds, settings.retry_backoff_seconds * 2**retries)
    return delay / 2 + random.uniform(0, delay / 2)  # noqa: S311 - jitter, not security


def retry_delay(
    exc: BaseException, retries: int, settings: WorkerSettings, now: datetime | None = None
) -> float | None:
    """Seconds until the next attempt, or None if ``exc`` should not be retried.

    Capped below the broker's visibility timeout: Redis would otherwise hand a task that is still
    waiting for its countdown to a second worker.
    """
    if not is_retryable(exc):
        return None
    if isinstance(exc, RateLimited):
        wait = (exc.reset_at - (now or datetime.now(UTC))).total_seconds() + random.uniform(1, 5)  # noqa: S311
    elif isinstance(exc, RetryableError) and exc.retry_after is not None:
        wait = exc.retry_after
    else:
        wait = backoff(retries, settings)
    return max(1.0, min(wait, settings.visibility_timeout_seconds * 0.9))


class IdempotencyLock:
    """``SET NX`` lock plus a "done" marker for one unit of work, owned by a Celery task id."""

    def __init__(
        self,
        client: redis.Redis,
        task: str,
        key: str,
        *,
        owner: str,
        ttl: int,
        done_ttl: int,
    ) -> None:
        self._redis = client
        self.lock_key = f"idem:lock:{task}:{key}"
        self.done_key = f"idem:done:{task}:{key}"
        self._owner = owner
        self._ttl = ttl
        self._done_ttl = done_ttl

    def acquire(self) -> Literal["acquired", "done", "busy"]:
        if self._redis.exists(self.done_key):
            return "done"
        if self._redis.set(self.lock_key, self._owner, nx=True, ex=self._ttl):
            return "acquired"
        if self._redis.get(self.lock_key) == self._owner:
            # Our own earlier attempt (a redelivery after the worker died, or a retry).
            self._redis.expire(self.lock_key, self._ttl)
            return "acquired"
        return "busy"

    def complete(self) -> None:
        self._redis.set(self.done_key, self._owner, ex=self._done_ttl)
        self.release()

    def release(self) -> None:
        """Delete the lock if we still own it (it may have expired and been taken)."""
        with self._redis.pipeline() as pipe:  # pyright: ignore[reportUnknownMemberType]
            try:
                pipe.watch(self.lock_key)
                if pipe.get(self.lock_key) == self._owner:
                    pipe.multi()
                    pipe.delete(self.lock_key)
                    pipe.execute()
                else:
                    pipe.unwatch()
            except redis.exceptions.WatchError:
                pass


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


# Task is generic only in the type stubs.
_TaskBase = Task[..., Any] if TYPE_CHECKING else Task


class SenseiTask(_TaskBase):
    requires: ClassVar[tuple[str, ...]] = ()
    idempotent_on: ClassVar[tuple[str, ...]] = ()
    system: ClassVar[bool] = False
    # How long "done" markers keep later duplicates away.
    idempotency_window_seconds: ClassVar[int] = 86_400

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        missing = [name for name in self.requires if kwargs.get(name) is None]
        if missing:
            msg = f"{self.name} needs keyword arguments {missing}"
            raise ValueError(msg)
        task_id = self.request.id or f"local-{uuid.uuid4()}"
        with bind_context(
            task=self.name,
            task_id=task_id,
            org_id=kwargs.get("org_id"),
            repo_id=kwargs.get("repo_id"),
            job_id=kwargs.get("job_id"),
        ):
            lock = self._lock(task_id, kwargs)
            if lock is not None:
                state = lock.acquire()
                if state != "acquired":
                    log.info("duplicate task skipped", reason=state)
                    return None
            try:
                result = self.run(*args, **kwargs)
            except Retry:
                raise
            except Exception as exc:
                if lock is not None:
                    lock.release()
                self._maybe_retry(exc)
                raise
            if lock is not None:
                lock.complete()
            return result

    def _lock(self, task_id: str, kwargs: dict[str, Any]) -> IdempotencyLock | None:
        if not self.idempotent_on:
            return None
        key = ":".join(str(kwargs.get(name)) for name in self.idempotent_on)
        settings = get_runtime().settings.worker
        return IdempotencyLock(
            get_runtime().sync_redis,
            self.name,
            key,
            owner=task_id,
            ttl=settings.time_limit_seconds + 60,
            done_ttl=self.idempotency_window_seconds,
        )

    def _maybe_retry(self, exc: Exception) -> None:
        settings = get_runtime().settings.worker
        limit = self.max_retries if self.max_retries is not None else settings.max_retries
        retries = int(self.request.retries or 0)
        delay = retry_delay(exc, retries, settings)
        if delay is None or retries >= limit:
            return
        log.warning(
            "task will be retried",
            error_type=type(exc).__name__,
            attempt=retries + 1,
            delay_seconds=round(delay, 1),
        )
        raise self.retry(exc=exc, countdown=delay, max_retries=limit)

    # ------------------------------------------------------------------ sessions

    @asynccontextmanager
    async def org_session(self, org_id: uuid.UUID | str) -> AsyncGenerator[AsyncSession]:
        """App-role session scoped to ``org_id`` (row-level security enforced)."""
        async with get_runtime().database.session() as session:
            await set_org_context(session, uuid.UUID(str(org_id)))
            yield session

    @asynccontextmanager
    async def system_session(self) -> AsyncGenerator[AsyncSession]:
        if not self.system:
            msg = f"{self.name} is not a system task; use org_session()"
            raise PermissionError(msg)
        async with get_runtime().system_database.session() as session:
            yield session

    # ------------------------------------------------------------------ dead letters

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: ExceptionInfo,
    ) -> None:
        values = {
            "task": self.name[:200],
            "task_id": str(task_id)[:64],
            "args": redact({"args": list(args), "kwargs": kwargs}),
            "org_id": _uuid_or_none(kwargs.get("org_id")),
            "repo_id": _uuid_or_none(kwargs.get("repo_id")),
            "error_type": type(exc).__name__[:200],
            "error": str(exc)[:MAX_TEXT] or type(exc).__name__,
            "traceback": str(einfo)[:MAX_TEXT] if einfo else None,
            "attempts": int(self.request.retries or 0) + 1,
        }
        log.error("task failed", error_type=values["error_type"], attempts=values["attempts"])
        runtime = get_runtime()

        async def _record() -> None:
            async with runtime.system_database.session() as session:
                await session.execute(insert(JobFailure).values(**values))

        try:
            runtime.run(_record())
        except Exception:
            log.exception("could not record the task failure")
