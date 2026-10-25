"""At-least-once delivery with a real worker: a task whose process dies mid-run is redelivered and
completes exactly once; one that keeps killing its process, or hits the hard time limit, is
dead-lettered.

Each test runs a Celery worker subprocess against its own Redis databases (broker, results, task
state: one of :data:`SLOTS`, leased for the test so two test sessions on one Redis never share
them) and the test database, so it never touches dev data. With every slot leased, it skips.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import redis
from celery import Celery
from pydantic import SecretStr
from sqlalchemy import delete, select

from sensei_core.config import RedisSettings, Settings, WorkerSettings, with_redis_db
from sensei_core.db import make_system_database
from sensei_core.jobs import make_celery
from sensei_core.models import JobFailure

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SUPPORT = Path(__file__).parent / "support"
# Broker, results and task state databases; dev uses 0 to 2, and 3 holds the slots' leases.
SLOTS = ((4, 5, 6), (7, 8, 9), (10, 11, 12), (13, 14, 15))
LEASE_DB = 3
LEASE_SECONDS = 900  # longer than a test takes


@dataclass
class Stack:
    producer: Celery
    state: redis.Redis
    worker: subprocess.Popen[bytes]
    log: Path

    def wait_for(self, check: Callable[[], bool], timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            if self.worker.poll() is not None:
                pytest.fail(f"worker exited early:\n{self.log.read_text()[-4000:]}")
            time.sleep(0.2)
        pytest.fail(f"timed out waiting for {what}:\n{self.log.read_text()[-4000:]}")


StartWorker = Callable[..., Stack]


@pytest.fixture
def slot(migrated_test_db: Settings) -> Iterator[tuple[int, int, int]]:
    """A free slot of Redis databases, leased for the test (skipped when every one is taken)."""
    base = migrated_test_db.redis.url.get_secret_value()
    leases = redis.Redis.from_url(with_redis_db(base, LEASE_DB), decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    owner = uuid.uuid4().hex
    for index, found in enumerate(SLOTS):
        key = f"sensei-test:redelivery-slot:{index}"
        if leases.set(key, owner, nx=True, ex=LEASE_SECONDS):
            try:
                yield found
            finally:
                if leases.get(key) == owner:
                    leases.delete(key)
                leases.close()
            return
    leases.close()
    pytest.skip("every Redis slot for worker tests is leased by another test session")


@pytest.fixture
def start_worker(
    tmp_path: Path, migrated_test_db: Settings, slot: tuple[int, int, int]
) -> Iterator[StartWorker]:
    base = migrated_test_db.redis.url.get_secret_value()
    broker_db, results_db, state_db = slot
    urls = {db: with_redis_db(base, db) for db in slot}
    clients = [redis.Redis.from_url(url, decode_responses=True) for url in urls.values()]  # pyright: ignore[reportUnknownMemberType]
    for client in clients:
        client.flushdb()  # pyright: ignore[reportUnknownMemberType]
    started: list[Stack] = []

    def start(**env_overrides: str) -> Stack:
        database = migrated_test_db.database
        env = {
            **os.environ,
            "SENSEI_ENV": "test",
            "SENSEI_REDIS_URL": urls[state_db],
            "SENSEI_WORKER_BROKER_URL": urls[broker_db],
            "SENSEI_WORKER_RESULT_BACKEND": urls[results_db],
            "SENSEI_DATABASE_URL": database.url.get_secret_value(),
            "SENSEI_DATABASE_SYSTEM_URL": database.system_url.get_secret_value(),
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(SUPPORT), os.environ.get("PYTHONPATH")])
            ),
            **env_overrides,
        }
        log = tmp_path / f"worker-{len(started)}.log"
        with log.open("wb") as out:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "redelivery_worker",
                    "worker",
                    "--pool=prefork",
                    "--concurrency=1",
                    "--queues=default",
                    "--loglevel=INFO",
                    "--without-gossip",
                    "--without-mingle",
                    "--without-heartbeat",
                ],
                cwd=SUPPORT,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        producer = make_celery(
            Settings(
                env="test",
                redis=RedisSettings(url=SecretStr(urls[state_db])),
                worker=WorkerSettings(
                    broker_url=SecretStr(urls[broker_db]),
                    result_backend=SecretStr(urls[results_db]),
                ),
            ),
            main="redelivery-test",
        )
        stack = Stack(producer=producer, state=clients[2], worker=worker, log=log)
        started.append(stack)
        return stack

    yield start

    for stack in started:
        stack.worker.send_signal(signal.SIGTERM)
        try:
            stack.worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            stack.worker.kill()
            stack.worker.wait()
        _close_producer(stack.producer)
    for client in clients:
        client.flushdb()  # pyright: ignore[reportUnknownMemberType]
        client.close()


def _close_producer(producer: Celery) -> None:
    """Celery.close() closes the broker pool only; sending a task also subscribed to its result
    on a Redis pub/sub connection. Close that and the result backend's client too."""
    backend = producer.backend
    consumer = getattr(backend, "result_consumer", None)
    if consumer is not None:
        consumer.stop()
        # A result object collected later unsubscribes; on a closed pub/sub that would quietly
        # open a new connection. With no pub/sub at all, it does nothing.
        setattr(consumer, "_pubsub", None)  # noqa: B010 - Celery has no public way to drop it
    client = getattr(backend, "client", None)
    if client is not None:
        client.connection_pool.disconnect()
    producer.close()


def _failure(settings: Settings, task_id: str, *, remove: bool = False) -> JobFailure | None:
    """The job_failures row for ``task_id`` (removed afterwards with ``remove``)."""

    async def read() -> JobFailure | None:
        database = make_system_database(settings.database)
        try:
            async with database.session() as db:
                row = await db.scalar(select(JobFailure).where(JobFailure.task_id == task_id))
                if remove:
                    await db.execute(delete(JobFailure).where(JobFailure.task_id == task_id))
                return row
        finally:
            await database.dispose()

    return asyncio.run(read())


def test_task_killed_mid_run_is_redelivered_and_completes_once(start_worker: StartWorker) -> None:
    # A short heartbeat, so the redelivery doesn't wait long for the dead attempt's to lapse.
    stack = start_worker(SENSEI_WORKER_LOCK_HEARTBEAT_SECONDS="1")
    marker = f"redelivery:{uuid.uuid4().hex}"
    kwargs = {"job_id": "job-1", "marker": marker}
    first = stack.producer.send_task("sensei.default.test_hang_once", kwargs=kwargs)

    stack.wait_for(lambda: stack.state.get(f"{marker}:pid:1") is not None, 60, "the first attempt")
    first_pid = int(str(stack.state.get(f"{marker}:pid:1")))
    os.kill(first_pid, signal.SIGKILL)  # the pool process dies mid-task

    stack.wait_for(lambda: stack.state.get(f"{marker}:completions") == "1", 60, "the redelivery")
    assert stack.state.get(f"{marker}:attempts") == "2"
    assert int(str(stack.state.get(f"{marker}:pid:2"))) != first_pid  # a fresh pool process
    assert first.get(timeout=30) == "done job-1"  # same task id, finished by the redelivery

    # The same work enqueued again is recognized as done and skipped.
    again = stack.producer.send_task("sensei.default.test_hang_once", kwargs=kwargs)
    assert again.get(timeout=30) is None
    assert stack.state.get(f"{marker}:completions") == "1"
    assert stack.state.get(f"{marker}:attempts") == "2"


def test_a_task_that_keeps_killing_its_worker_is_dead_lettered(
    start_worker: StartWorker, migrated_test_db: Settings
) -> None:
    stack = start_worker(SENSEI_WORKER_MAX_DELIVERIES="2")
    marker = f"poison:{uuid.uuid4().hex}"
    result = stack.producer.send_task("sensei.default.test_always_dies", kwargs={"marker": marker})
    # Two deaths, then the third delivery is refused, dead-lettered and acknowledged.
    stack.wait_for(lambda: _failure(migrated_test_db, result.id) is not None, 60, "the dead letter")
    assert stack.state.get(f"{marker}:deliveries") == "2"
    row = _failure(migrated_test_db, result.id, remove=True)
    assert row is not None
    assert (row.task, row.error_type) == ("sensei.default.test_always_dies", "WorkerLost")


def test_a_hard_time_limit_kill_is_dead_lettered(
    start_worker: StartWorker, migrated_test_db: Settings
) -> None:
    stack = start_worker(
        SENSEI_WORKER_SOFT_TIME_LIMIT_SECONDS="1",
        SENSEI_WORKER_TIME_LIMIT_SECONDS="3",
        SENSEI_WORKER_VISIBILITY_TIMEOUT_SECONDS="300",
    )
    marker = f"timeout:{uuid.uuid4().hex}"
    result = stack.producer.send_task(
        "sensei.default.test_ignores_soft_limit", kwargs={"marker": marker}
    )
    stack.wait_for(lambda: stack.state.get(f"{marker}:started") == "1", 60, "the task to start")
    stack.wait_for(lambda: _failure(migrated_test_db, result.id) is not None, 60, "the dead letter")
    row = _failure(migrated_test_db, result.id, remove=True)
    assert row is not None
    assert row.error_type == "TimeLimitExceeded"
    assert "hard time limit (3s)" in row.error
