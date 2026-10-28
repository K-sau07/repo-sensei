"""The base class every repo-sensei task uses.

Declared on the task decorator::

    @celery_app.task(bind=True, base=SenseiTask, name=..., requires=("org_id",),
                     idempotent_on=("job_id",), system=False)

* ``requires``: keyword arguments that must be present (refused before running otherwise).
* ``idempotent_on``: keyword arguments naming the unit of work. Delivery is at least once, so a
  task can run twice. A Redis lock is owned by the Celery task id and kept alive by a heartbeat
  while the task runs: another enqueue of the same work (another id) skips; a second copy of the
  same message (Redis can hand it out again) waits while the heartbeat is fresh, and takes over
  only once it has stopped (the process running it died). Finished work leaves a "done" marker
  so later duplicates skip too.
* ``system``: the task may open system-role sessions (row-level security bypassed). Only for
  explicitly named cross-org work; other tasks get org-scoped sessions.

Failures are classified: network errors, 5xx, rate limits and :class:`RetryableError` are retried
with exponential backoff and jitter; anything else, or running out of retries, is final and is
written to ``job_failures`` (as are hard time-limit kills, and an attempt whose process keeps
dying). Retry delays stay below ``max_countdown_seconds`` so Redis never redelivers a waiting or
running task.
"""

from __future__ import annotations

import math
import random
import threading
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NoReturn, cast

import botocore.exceptions
import httpx
import redis
import redis.exceptions
import sqlalchemy.exc
from billiard.einfo import ExceptionInfo
from celery import Task
from celery.exceptions import Retry
from celery.worker import state as worker_state
from celery.worker.request import Request
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import WorkerSettings
from sensei_core.context import bind_context
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.client import GitHubError, RateLimited
from sensei_core.logging import get_logger
from sensei_core.models import JobFailure
from sensei_core.redaction import redact, redact_text
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)

# Celery needs a number to retry by; a task waits this many times at most (a day of minute-long
# waits is 1,440), each wait bounded by the caller's own deadline.
WAITS = 100_000
# Error retries are counted per task id (waits aren't errors), kept past the longest wait.
ERROR_RETRIES_PREFIX = "error-retries:"
ERROR_RETRIES_TTL_SECONDS = 3 * 86_400

MAX_TEXT = 20_000


class NotYetError(Exception):
    """The task came too early (what it needs isn't there yet): nothing is done, nothing is
    retried, and it isn't recorded as done, so the delivery that comes at the right time runs."""


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
    # Object storage (botocore's own, not the builtins): no connection, a connect or read
    # timeout, a connection reset part way.
    botocore.exceptions.ConnectionError,
    botocore.exceptions.HTTPClientError,
)
# Object storage answering "slow down" or "not now" (S3, R2, MinIO), whatever the status.
_STORAGE_THROTTLED = frozenset({
    "SlowDown", "Throttling", "ThrottlingException", "RequestTimeout", "RequestTimeoutException",
    "TooManyRequestsException", "RequestLimitExceeded", "ServiceUnavailable", "InternalError",
})  # fmt: skip


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (RetryableError, RateLimited, *_TRANSIENT)):
        return True
    if isinstance(exc, GitHubError):
        return exc.status >= 500 or exc.status == 429
    if isinstance(exc, botocore.exceptions.ClientError):
        error = exc.response.get("Error", {})
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return status >= 500 or status == 429 or error.get("Code") in _STORAGE_THROTTLED
    return isinstance(exc, sqlalchemy.exc.DBAPIError) and exc.connection_invalidated


def backoff(retries: int, settings: WorkerSettings) -> float:
    """Exponential with "equal jitter": half the delay is fixed, half random."""
    delay = min(settings.retry_backoff_max_seconds, settings.retry_backoff_seconds * 2**retries)
    return delay / 2 + random.uniform(0, delay / 2)  # noqa: S311 - jitter, not security


def retry_delay(
    exc: BaseException, retries: int, settings: WorkerSettings, now: datetime | None = None
) -> float | None:
    """Seconds until the next attempt, or None if ``exc`` should not be retried.

    Capped so that the wait plus the longest possible run stays inside the broker's visibility
    timeout: Redis would otherwise hand the same message to a second worker.
    """
    if not is_retryable(exc):
        return None
    if isinstance(exc, RateLimited):
        wait = (exc.reset_at - (now or datetime.now(UTC))).total_seconds() + random.uniform(1, 5)  # noqa: S311
    elif isinstance(exc, RetryableError) and exc.retry_after is not None:
        wait = exc.retry_after
    else:
        wait = backoff(retries, settings)
    return max(1.0, min(wait, settings.max_countdown_seconds))


LockState = Literal["acquired", "done", "busy", "running"]


class IdempotencyLock:
    """``SET NX`` lock plus a "done" marker for one unit of work, owned by a Celery task id, with
    a heartbeat key that only exists while the owner is alive.

    ``acquire`` answers ``busy`` when other work holds it and ``running`` when the owner is this
    same task id with a live heartbeat (a second copy of the message: wait, don't run)."""

    def __init__(
        self,
        client: redis.Redis,
        task: str,
        key: str,
        *,
        owner: str,
        ttl: int,
        done_ttl: int,
        heartbeat: float,
    ) -> None:
        self._redis = client
        self.lock_key = f"idem:lock:{task}:{key}"
        self.done_key = f"idem:done:{task}:{key}"
        self.beat_key = f"idem:beat:{task}:{key}"
        self._owner = owner
        self._ttl = ttl
        self._done_ttl = done_ttl
        self._heartbeat = heartbeat
        self.beat_ttl = max(1, math.ceil(heartbeat * 3))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> LockState:
        if self._redis.exists(self.done_key):
            return "done"
        if self._redis.set(self.lock_key, self._owner, nx=True, ex=self._ttl):
            self._start_heartbeat()
            return "acquired"
        if self._redis.get(self.lock_key) != self._owner:
            return "busy"
        if self._redis.exists(self.beat_key):
            return "running"  # the same message is still being worked on elsewhere
        # Our own attempt whose process died: take over.
        self._redis.expire(self.lock_key, self._ttl)
        self._start_heartbeat()
        return "acquired"

    def complete(self) -> None:
        self._redis.set(self.done_key, self._owner, ex=self._done_ttl)
        self.release()

    def _beat(self) -> None:
        self._redis.set(self.beat_key, self._owner, ex=self.beat_ttl)
        self._redis.expire(self.lock_key, self._ttl)

    def _start_heartbeat(self) -> None:
        self._beat()

        def loop() -> None:
            while not self._stop.wait(self._heartbeat):
                try:
                    self._beat()
                except redis.exceptions.RedisError:
                    log.warning("lock heartbeat failed", lock=self.lock_key)

        self._thread = threading.Thread(target=loop, name="sensei-lock-heartbeat", daemon=True)
        self._thread.start()

    def _stop_heartbeat(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._redis.delete(self.beat_key)

    def release(self) -> None:
        """Stop the heartbeat and delete the lock if we still own it (it may have expired and
        been taken)."""
        self._stop_heartbeat()
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
            if self._delivered_too_often(task_id, args, kwargs):
                return None
            lock = self._lock(task_id, kwargs)
            if lock is not None:
                state = lock.acquire()
                if state == "running":
                    log.info("same task already running; checking back later")
                    self.wait_again(lock.beat_ttl + 1)
                if state != "acquired":
                    log.info("duplicate task skipped", reason=state)
                    return None
            try:
                result = self.run(*args, **kwargs)
            except NotYetError as exc:
                if lock is not None:
                    lock.release()
                log.info("task came too early; skipped", reason=str(exc))
                return None
            except Retry:
                if lock is not None:
                    lock.release()
                raise
            except Exception as exc:
                if lock is not None:
                    lock.release()
                self._maybe_retry(exc, task_id)
                raise
            if lock is not None:
                lock.complete()
            return result

    def _delivered_too_often(
        self, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> bool:
        """Count deliveries of this attempt. One whose process keeps dying (out of memory, a
        crash in native code) would otherwise come back forever; past the limit it is
        dead-lettered and acknowledged."""
        if self.request.is_eager:
            return False
        settings = get_runtime().settings.worker
        key = f"deliveries:{task_id}:{int(self.request.retries or 0)}"
        client = get_runtime().sync_redis
        count = int(client.incr(key))  # pyright: ignore[reportArgumentType]
        client.expire(key, 86_400)
        if count <= settings.max_deliveries:
            return False
        error = f"the process running this task died {count - 1} times"
        log.error("task dead-lettered after repeated worker loss", deliveries=count - 1)
        self.record_failure(
            task_id, args, kwargs, error_type="WorkerLost", error=error, traceback=None
        )
        self.on_dead_letter(task_id, args, kwargs)
        return True

    def on_dead_letter(self, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        """Called when a delivery is dropped for good after repeated worker loss (no
        ``on_failure`` runs then). For subclasses that must record the outcome elsewhere."""

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
            heartbeat=settings.lock_heartbeat_seconds,
        )

    def wait_again(self, countdown: float, exc: Exception | None = None) -> NoReturn:
        """Run again in ``countdown`` seconds: work that is waiting (a batch still running, a
        rate limit, another copy of the task), not failing, so no retry limit applies. (Celery
        reads ``max_retries=None`` as "the task's limit", which a long wait would run past.)"""
        raise self.retry(exc=exc, countdown=countdown, max_retries=WAITS)

    def _error_retries(self, task_id: str) -> int:
        """This task's retries after an error so far. Celery's ``request.retries`` also counts
        waits (:meth:`wait_again`), so an error late in a long wait would find the limit spent;
        errors are counted apart, by task id (a retry keeps the id). Without Redis, Celery's
        count stands in (waits included: the stricter limit)."""
        try:
            found = get_runtime().sync_redis.get(f"{ERROR_RETRIES_PREFIX}{task_id}")
        except redis.exceptions.RedisError as exc:
            log.warning("error retries unreadable; counting waits too", error=type(exc).__name__)
            return int(self.request.retries or 0)
        return int(cast("Any", found) or 0)

    def _count_error_retry(self, task_id: str) -> None:
        key = f"{ERROR_RETRIES_PREFIX}{task_id}"
        try:
            client = get_runtime().sync_redis
            client.incr(key)
            client.expire(key, ERROR_RETRIES_TTL_SECONDS)
        except redis.exceptions.RedisError as exc:
            log.warning("error retry not counted", error=type(exc).__name__)

    def _maybe_retry(self, exc: Exception, task_id: str) -> None:
        """Retry after ``exc`` if it is retryable and the limit isn't reached; ``task_id``: the
        run's (as :meth:`__call__` resolved it: a direct call has no Celery id)."""
        settings = get_runtime().settings.worker
        limit = self.max_retries if self.max_retries is not None else settings.max_retries
        retries = self._error_retries(task_id)
        delay = retry_delay(exc, retries, settings)
        if delay is None or retries >= limit:
            return
        self._count_error_retry(task_id)
        log.warning(
            "task will be retried",
            error_type=type(exc).__name__,
            attempt=retries + 1,
            delay_seconds=round(delay, 1),
        )
        self.on_retry_scheduled(dict(self.request.kwargs or {}), delay)
        # The limit is ours (above); Celery's would count the waits too.
        raise self.retry(exc=exc, countdown=delay, max_retries=WAITS)

    def on_retry_scheduled(self, kwargs: dict[str, Any], delay: float) -> None:
        """Called when the task will run again in ``delay`` seconds. For subclasses whose work
        is watched for stalls."""

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
        log.error("task failed", error_type=type(exc).__name__)
        self.record_failure(
            task_id,
            args,
            kwargs,
            error_type=type(exc).__name__,
            error=str(exc) or type(exc).__name__,
            traceback=str(einfo) if einfo else None,
        )

    def on_hard_timeout(self, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        """Called in the worker's main process when the pool process was killed at the hard time
        limit (Celery acknowledges the message without calling ``on_failure``)."""
        limit = get_runtime().settings.worker.time_limit_seconds
        self.record_failure(
            task_id,
            args,
            kwargs,
            error_type="TimeLimitExceeded",
            error=f"killed at the hard time limit ({limit}s)",
            traceback=None,
        )
        lock = self._lock(task_id, kwargs)
        if lock is not None:
            lock.release()

    def record_failure(
        self,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        error_type: str,
        error: str,
        traceback: str | None,
    ) -> None:
        """Write the dead letter. Messages, tracebacks and arguments are redacted: database and
        HTTP errors quote parameters and URLs."""
        values = {
            "task": self.name[:200],
            "task_id": str(task_id)[:64],
            "args": redact({"args": list(args), "kwargs": kwargs}),
            "org_id": _uuid_or_none(kwargs.get("org_id")),
            "repo_id": _uuid_or_none(kwargs.get("repo_id")),
            "error_type": error_type[:200],
            "error": redact_text(error)[:MAX_TEXT],
            "traceback": redact_text(traceback)[:MAX_TEXT] if traceback else None,
            "attempts": int(self.request.retries or 0) + 1,
        }
        runtime = get_runtime()

        async def _record() -> None:
            async with runtime.system_database.session() as session:
                await session.execute(insert(JobFailure).values(**values))

        try:
            runtime.run(_record())
        except Exception:
            log.exception("could not record the task failure")


class SenseiRequest(Request):
    """Dead-letters tasks killed at the hard time limit (``SenseiTask.Request``)."""

    def on_timeout(self, soft: bool, timeout: float) -> None:
        super().on_timeout(soft, timeout)
        if soft or worker_state.should_terminate:
            return
        task = self.task
        if isinstance(task, SenseiTask):
            task.on_hard_timeout(self.id, tuple(self.args), dict(self.kwargs))


SenseiTask.Request = f"{SenseiRequest.__module__}:{SenseiRequest.__qualname__}"
