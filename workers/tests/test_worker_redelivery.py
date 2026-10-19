"""At-least-once delivery with a real worker: a task whose process dies mid-run is redelivered and
completes exactly once.

Runs a Celery worker subprocess against its own Redis databases (broker 11, results 12, task state
13), so it never touches dev data.
"""

from __future__ import annotations

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

from sensei_core.config import RedisSettings, Settings, WorkerSettings, with_redis_db
from sensei_core.jobs import make_celery

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SUPPORT = Path(__file__).parent / "support"
TASK = "sensei.default.test_hang_once"
BROKER_DB, RESULTS_DB, STATE_DB = 11, 12, 13


@dataclass
class Stack:
    producer: Celery
    state: redis.Redis
    worker: subprocess.Popen[bytes]
    log: Path


def _wait_for(check: Callable[[], bool], timeout: float, what: str, stack: Stack) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        if stack.worker.poll() is not None:
            pytest.fail(f"worker exited early:\n{stack.log.read_text()[-4000:]}")
        time.sleep(0.2)
    pytest.fail(f"timed out waiting for {what}:\n{stack.log.read_text()[-4000:]}")


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    base = Settings(env="test").redis.url.get_secret_value()
    urls = {db: with_redis_db(base, db) for db in (BROKER_DB, RESULTS_DB, STATE_DB)}
    clients = [redis.Redis.from_url(url, decode_responses=True) for url in urls.values()]  # pyright: ignore[reportUnknownMemberType]
    for client in clients:
        client.flushdb()  # pyright: ignore[reportUnknownMemberType]

    env = {
        **os.environ,
        "SENSEI_ENV": "test",
        "SENSEI_REDIS_URL": urls[STATE_DB],
        "SENSEI_WORKER_BROKER_URL": urls[BROKER_DB],
        "SENSEI_WORKER_RESULT_BACKEND": urls[RESULTS_DB],
        "PYTHONPATH": os.pathsep.join(filter(None, [str(SUPPORT), os.environ.get("PYTHONPATH")])),
    }
    log = tmp_path / "worker.log"
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
            redis=RedisSettings(url=SecretStr(urls[STATE_DB])),
            worker=WorkerSettings(
                broker_url=SecretStr(urls[BROKER_DB]),
                result_backend=SecretStr(urls[RESULTS_DB]),
            ),
        ),
        main="redelivery-test",
    )
    try:
        yield Stack(producer=producer, state=clients[2], worker=worker, log=log)
    finally:
        worker.send_signal(signal.SIGTERM)
        try:
            worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
        producer.close()
        for client in clients:
            client.flushdb()  # pyright: ignore[reportUnknownMemberType]
            client.close()


def test_task_killed_mid_run_is_redelivered_and_completes_once(stack: Stack) -> None:
    marker = f"redelivery:{uuid.uuid4().hex}"
    kwargs = {"job_id": "job-1", "marker": marker}
    first = stack.producer.send_task(TASK, kwargs=kwargs, queue="default")

    _wait_for(
        lambda: stack.state.get(f"{marker}:pid:1") is not None, 60, "the first attempt", stack
    )
    first_pid = int(str(stack.state.get(f"{marker}:pid:1")))
    os.kill(first_pid, signal.SIGKILL)  # the pool process dies mid-task

    _wait_for(lambda: stack.state.get(f"{marker}:completions") == "1", 60, "the redelivery", stack)
    assert stack.state.get(f"{marker}:attempts") == "2"
    assert int(str(stack.state.get(f"{marker}:pid:2"))) != first_pid  # a fresh pool process
    assert first.get(timeout=30) == "done job-1"  # same task id, finished by the redelivery

    # The same work enqueued again is recognized as done and skipped.
    again = stack.producer.send_task(TASK, kwargs=kwargs, queue="default")
    assert again.get(timeout=30) is None
    assert stack.state.get(f"{marker}:completions") == "1"
    assert stack.state.get(f"{marker}:attempts") == "2"
