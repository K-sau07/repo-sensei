"""SenseiTask in eager mode: required arguments, retry classification and idempotency."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis
import httpx
import pytest
import sqlalchemy.exc
from celery import Celery

from sensei_core.config import Settings, WorkerSettings
from sensei_core.db import Database
from sensei_core.github.client import GitHubError, RateLimited
from sensei_core.jobs import (
    TASK_ROUTES,
    Queue,
    broker_url,
    celery_config,
    make_celery,
    result_backend,
)
from sensei_workers.base import (
    IdempotencyLock,
    RetryableError,
    SenseiTask,
    backoff,
    is_retryable,
    retry_delay,
)
from sensei_workers.runtime import Runtime, set_runtime

WORKER = WorkerSettings(always_eager=True, max_retries=3)


class FakeRuntime(Runtime):
    """Locks in fakeredis; no database (failure records are covered by integration tests)."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.fake = fakeredis.FakeRedis(decode_responses=True)

    @property
    def sync_redis(self) -> Any:
        return self.fake

    @property
    def system_database(self) -> Database:
        msg = "no database in unit tests"
        raise RuntimeError(msg)


@pytest.fixture
def runtime() -> Iterator[FakeRuntime]:
    fake = FakeRuntime(Settings(env="test", worker=WORKER))
    set_runtime(fake)
    yield fake
    set_runtime(None)


@pytest.fixture
def app(runtime: FakeRuntime) -> Celery:
    return make_celery(runtime.settings, main="sensei-test")


# ------------------------------------------------------------------ configuration


def test_reliability_settings() -> None:
    config = celery_config(Settings(env="test"))
    assert config["task_acks_late"] is True
    assert config["task_reject_on_worker_lost"] is True
    assert config["worker_prefetch_multiplier"] == 1
    assert config["task_serializer"] == config["result_serializer"] == "json"
    assert config["accept_content"] == ["json"]
    assert config["result_expires"] == 86_400
    timeout = config["broker_transport_options"]["visibility_timeout"]
    assert config["task_soft_time_limit"] < config["task_time_limit"] < timeout


def test_broker_and_results_use_their_own_redis_databases() -> None:
    settings = Settings(env="test")
    assert broker_url(settings) == "redis://localhost:56379/1"
    assert result_backend(settings) == "redis://localhost:56379/2"


def test_limits_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="soft time limit < time limit < visibility timeout"):
        WorkerSettings(time_limit_seconds=4000, visibility_timeout_seconds=3600)


def test_every_queue_has_a_route(app: Celery) -> None:
    assert {route["queue"] for route in TASK_ROUTES.values()} == {q.value for q in Queue}
    for queue in Queue:
        routed = app.amqp.router.route({}, f"sensei.{queue.value}.anything")
        assert routed["queue"].name == queue.value
    assert app.amqp.router.route({}, "elsewhere.task")["queue"].name == "default"


# ------------------------------------------------------------------ retry classification


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (RetryableError("later"), True),
        (httpx.ConnectError("refused"), True),
        (httpx.ReadTimeout("slow"), True),
        (ConnectionResetError(), True),
        (TimeoutError(), True),
        (GitHubError(502, "bad gateway"), True),
        (GitHubError(429, "slow down"), True),
        (RateLimited(NOW), True),
        (sqlalchemy.exc.OperationalError("select 1", {}, Exception("server closed")), True),
        (GitHubError(404, "not found"), False),
        (GitHubError(403, "forbidden"), False),
        (ValueError("bad input"), False),
        (KeyError("missing"), False),
        (sqlalchemy.exc.IntegrityError("insert", {}, Exception("duplicate")), False),
    ],
)
def test_retry_classification(exc: BaseException, retryable: bool) -> None:
    assert is_retryable(exc) is retryable
    assert (retry_delay(exc, 0, WORKER, NOW) is not None) is retryable


def test_backoff_grows_exponentially_with_jitter() -> None:
    for retries in range(8):
        full = min(WORKER.retry_backoff_max_seconds, WORKER.retry_backoff_seconds * 2**retries)
        delays = [backoff(retries, WORKER) for _ in range(50)]
        assert all(full / 2 <= d <= full for d in delays)
    assert len({round(backoff(3, WORKER), 6) for _ in range(20)}) > 1  # jittered


def test_rate_limits_wait_for_the_reset() -> None:
    delay = retry_delay(RateLimited(NOW + timedelta(seconds=90)), 0, WORKER, NOW)
    assert delay is not None
    assert 91 <= delay <= 95


def test_retry_after_is_respected() -> None:
    assert retry_delay(RetryableError("later", retry_after=7), 0, WORKER, NOW) == 7


def test_delays_stay_below_the_visibility_timeout() -> None:
    far = RateLimited(NOW + timedelta(hours=3))
    delay = retry_delay(far, 0, WORKER, NOW)
    assert delay is not None
    assert delay < WORKER.visibility_timeout_seconds


# ------------------------------------------------------------------ tasks


def test_required_arguments_are_enforced(app: Celery) -> None:
    @app.task(bind=True, base=SenseiTask, name="sensei.default.needs_org", requires=("org_id",))
    def needs_org(self: SenseiTask, **_: Any) -> str:
        return "ran"

    failed = needs_org.apply_async(kwargs={})
    assert failed.state == "FAILURE"
    assert isinstance(failed.result, ValueError)
    assert needs_org.apply_async(kwargs={"org_id": "o1"}).get() == "ran"


def test_retryable_errors_are_retried_until_success(app: Celery) -> None:
    attempts: list[int] = []

    @app.task(bind=True, base=SenseiTask, name="sensei.default.flaky")
    def flaky(self: SenseiTask) -> str:
        attempts.append(int(self.request.retries or 0))
        if len(attempts) < 3:
            msg = "github is having a moment"
            raise RetryableError(msg, retry_after=1)
        return "done"

    result = flaky.apply_async()
    assert (result.state, result.result) == ("SUCCESS", "done")
    assert attempts == [0, 1, 2]


def test_retries_stop_at_the_limit(app: Celery) -> None:
    attempts: list[int] = []

    @app.task(bind=True, base=SenseiTask, name="sensei.default.always_down")
    def always_down(self: SenseiTask) -> None:
        attempts.append(int(self.request.retries or 0))
        raise httpx.ConnectError("down")

    result = always_down.apply_async()
    assert result.state == "FAILURE"
    assert attempts == [0, 1, 2, 3]  # the first try and max_retries=3 retries


def test_other_errors_fail_immediately(app: Celery) -> None:
    attempts: list[int] = []

    @app.task(bind=True, base=SenseiTask, name="sensei.default.broken")
    def broken(self: SenseiTask) -> None:
        attempts.append(1)
        msg = "a bug"
        raise ValueError(msg)

    result = broken.apply_async()
    assert result.state == "FAILURE"
    assert attempts == [1]


def test_duplicates_of_finished_work_are_skipped(app: Celery, runtime: FakeRuntime) -> None:
    runs: list[str] = []

    @app.task(bind=True, base=SenseiTask, name="sensei.default.once", idempotent_on=("job_id",))
    def once(self: SenseiTask, *, job_id: str) -> str:
        runs.append(job_id)
        return "done"

    assert once.apply_async(kwargs={"job_id": "j1"}).get() == "done"
    assert once.apply_async(kwargs={"job_id": "j1"}).get() is None  # another enqueue, same work
    assert once.apply_async(kwargs={"job_id": "j2"}).get() == "done"
    assert runs == ["j1", "j2"]
    assert runtime.fake.exists("idem:done:sensei.default.once:j1") == 1
    assert runtime.fake.exists("idem:lock:sensei.default.once:j1") == 0


def test_failed_work_can_run_again(app: Celery) -> None:
    runs: list[int] = []

    @app.task(
        bind=True, base=SenseiTask, name="sensei.default.fails_once", idempotent_on=("job_id",)
    )
    def fails_once(self: SenseiTask, *, job_id: str) -> str:
        runs.append(1)
        if len(runs) == 1:
            msg = "bug fixed later"
            raise ValueError(msg)
        return "done"

    assert fails_once.apply_async(kwargs={"job_id": "j1"}).state == "FAILURE"
    assert fails_once.apply_async(kwargs={"job_id": "j1"}).get() == "done"


def test_lock_lets_the_owner_back_in(runtime: FakeRuntime) -> None:
    """A redelivered task keeps its id: it takes its own lock back. Another id waits it out."""
    first = IdempotencyLock(runtime.fake, "t", "k", owner="task-1", ttl=60, done_ttl=600)
    redelivered = IdempotencyLock(runtime.fake, "t", "k", owner="task-1", ttl=60, done_ttl=600)
    duplicate = IdempotencyLock(runtime.fake, "t", "k", owner="task-2", ttl=60, done_ttl=600)
    assert first.acquire() == "acquired"
    assert redelivered.acquire() == "acquired"
    assert duplicate.acquire() == "busy"
    duplicate.release()  # not the owner: the lock stays
    assert runtime.fake.get(first.lock_key) == "task-1"
    redelivered.complete()
    assert duplicate.acquire() == "done"
    assert runtime.fake.exists(first.lock_key) == 0


def test_system_sessions_are_only_for_system_tasks(app: Celery, runtime: FakeRuntime) -> None:
    @app.task(bind=True, base=SenseiTask, name="sensei.default.org_task")
    def org_task(self: SenseiTask) -> None:
        async def _open() -> None:
            async with self.system_session():
                pass

        runtime.run(_open())

    result = org_task.apply_async()
    assert result.state == "FAILURE"
    assert isinstance(result.result, PermissionError)
