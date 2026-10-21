"""The per-process runtime: one event loop, and nothing left running between tasks."""

from __future__ import annotations

import asyncio
import signal
import sys

import pytest

from sensei_core.config import Settings
from sensei_workers.runtime import Runtime


class SoftLimit(Exception):
    pass


def _raise_soft_limit(_signum: int, _frame: object) -> None:
    raise SoftLimit


@pytest.mark.skipif(sys.platform == "win32", reason="needs SIGALRM")
def test_an_interrupted_coroutine_does_not_resume_in_the_next_task() -> None:
    runtime = Runtime(Settings(env="test"))
    events: list[str] = []

    async def slow_task() -> None:
        await asyncio.sleep(0.5)  # waiting on I/O when the limit fires
        events.append("first task committed")  # must never happen

    previous = signal.signal(signal.SIGALRM, _raise_soft_limit)
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.05)
        with pytest.raises(SoftLimit):
            runtime.run(slow_task())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    async def next_task() -> None:
        await asyncio.sleep(0.8)
        events.append("second task done")

    runtime.run(next_task())
    assert events == ["second task done"]
    runtime.close()


def test_the_loop_is_reused_across_tasks() -> None:
    runtime = Runtime(Settings(env="test"))

    async def current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert runtime.run(current_loop()) is runtime.run(current_loop())
    runtime.close()
