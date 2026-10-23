"""Running per-file work in parallel processes, safe from files that crash the parser.

tree-sitter parsers aren't thread-safe and parsing is CPU-bound, so files are parsed in
a pool of processes (CPU count - 1 by default), sent in batches. ``work`` runs once per item in
a worker process; it must be a module-level function, and items and results must pickle.
Processes are spawned, never forked: the caller (a Celery worker) has threads and open
connections a fork would copy. Celery's prefork workers are daemonic processes, which
multiprocessing won't let start children; the flag is lifted while the pool runs (see
:func:`_children_allowed`).

A Python exception from ``work`` fails only its item (:class:`Failed`, reason ``error``). A
grammar bug can do worse: kill the process (a segmentation fault), which breaks the whole pool,
or never finish. When the pool breaks, or no batch finishes for ``stall_seconds`` (its workers
are then killed), the batches that were running are retried one item at a time in a
single-process pool: an item that crashes it again fails (reason ``crashed``), one that runs
past ``item_timeout`` has its process killed (reason ``timeout``), and the rest carry on in a
fresh pool. This is the only way to stop a parse: tree-sitter can't be interrupted from Python.
If worker processes can't run anything at all, :class:`WorkersUnavailableError` is raised
instead of marking every file.

Results come back as their batches finish, not in input order.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Generator, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass
from multiprocessing import process as mp_process
from typing import Any, Literal, cast

# multiprocessing's key type (bytes that only pickle while spawning); not in the type stubs.
_AuthenticationString = cast("type[bytes]", vars(mp_process)["AuthenticationString"])

BATCH_SIZE = 64
PARENT_CHECK_SECONDS = 1.0
IN_FLIGHT_PER_WORKER = 2  # batches queued per process (bounds the results held in memory)
STALL_SECONDS = 30.0  # no batch finished for this long: something hangs


class WorkersUnavailableError(Exception):
    """Worker processes die before doing any work: the pool can't run at all."""


@dataclass(frozen=True, slots=True)
class Failed:
    reason: Literal["error", "crashed", "timeout"]
    message: str


def default_workers() -> int:
    return max(1, (os.process_cpu_count() or 2) - 1)


def apply_one[T, R](work: Callable[[T], R], item: T) -> R | Failed:
    try:
        return work(item)
    except Exception as exc:  # one item's failure is recorded, never fatal
        return Failed("error", f"{type(exc).__name__}: {exc}"[:500])


def _apply_batch[T, R](work: Callable[[T], R], batch: list[T]) -> list[R | Failed]:
    return [apply_one(work, item) for item in batch]


def _exit_with_parent(parent: int) -> None:
    """In a worker: exit once the parent is gone (killed at a time limit, out of memory). The
    worker would otherwise wait for work forever, as it holds both ends of the pool's pipes."""

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(PARENT_CHECK_SECONDS)
        os._exit(1)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def _executor(workers: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_exit_with_parent,
        initargs=(os.getpid(),),
    )


@contextmanager
def _children_allowed() -> Generator[None]:
    """Let this process start children even if it is a billiard daemon (a Celery prefork
    worker).

    multiprocessing forbids it so that a daemon's children aren't orphaned when it is killed.
    Here the pool is shut down before the stage returns, and its workers exit on their own
    within a second of a killed parent (:func:`_exit_with_parent`), so none outlive it."""
    config = cast("dict[str, object]", vars(multiprocessing.current_process())["_config"])
    saved = dict(config)
    config.pop("daemon", None)
    # billiard keeps the key children authenticate with in its own type, which multiprocessing
    # refuses to send to a spawned process.
    authkey = config.get("authkey")
    if authkey is not None and not isinstance(authkey, _AuthenticationString):
        config["authkey"] = _AuthenticationString(cast("bytes", authkey))
    try:
        yield
    finally:
        config.clear()
        config.update(saved)


def _kill(executor: ProcessPoolExecutor) -> None:
    """Kill the pool's worker processes (the only way to stop a hung parse). The executor then
    finds itself broken and can be shut down."""
    processes = cast("dict[int, Any]", vars(executor).get("_processes") or {})
    for process in list(processes.values()):
        process.kill()


@dataclass(frozen=True, slots=True)
class _Limits:
    item_timeout: float | None
    stall_seconds: float


def run_parallel[T, R](
    work: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int | None = None,
    batch_size: int = BATCH_SIZE,
    item_timeout: float | None = None,
    stall_seconds: float = STALL_SECONDS,
) -> Iterator[tuple[T, R | Failed]]:
    """``(item, work(item))`` for every item, as batches finish. ``workers=0`` runs in this
    process (tests, debugging: no timeouts, and a crash takes the caller down)."""
    if workers == 0:
        for item in items:
            yield item, apply_one(work, item)
        return
    limits = _Limits(item_timeout, stall_seconds)
    with _children_allowed():
        yield from _run_pools(work, items, workers or default_workers(), batch_size, limits)


def _run_pools[T, R](
    work: Callable[[T], R], items: Sequence[T], size: int, batch_size: int, limits: _Limits
) -> Iterator[tuple[T, R | Failed]]:
    pending = deque(list(items[i : i + batch_size]) for i in range(0, len(items), batch_size))
    while pending:
        lost: list[list[T]] = []
        executor = _executor(size)
        try:
            running: dict[Future[list[R | Failed]], list[T]] = {}
            while pending or running:
                while pending and len(running) < size * IN_FLIGHT_PER_WORKER:
                    batch = pending.popleft()
                    running[executor.submit(_apply_batch, work, batch)] = batch
                finished, _ = wait(
                    running, timeout=limits.stall_seconds, return_when=FIRST_COMPLETED
                )
                if not finished:  # hung: retry what was running one item at a time
                    lost.extend(running.values())
                    _kill(executor)
                    break
                broken = False
                for future in finished:
                    batch = running.pop(future)
                    try:
                        results = future.result()
                    except BrokenProcessPool:
                        lost.append(batch)
                        broken = True
                        continue
                    yield from zip(batch, results, strict=True)
                if broken:
                    lost.extend(running.values())  # they fail the same way
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if lost:
            yield from _isolate(work, [item for batch in lost for item in batch], limits)


def _healthy_executor() -> ProcessPoolExecutor:
    """A fresh one-process pool that has shown it can run something: otherwise every item would
    look like it crashed when the processes can't even start."""
    executor = _executor(1)
    try:
        executor.submit(os.getpid).result()
    except BrokenProcessPool as exc:
        executor.shutdown(wait=True)
        raise WorkersUnavailableError("worker processes exit before running anything") from exc
    return executor


def _isolate[T, R](
    work: Callable[[T], R], items: list[T], limits: _Limits
) -> Iterator[tuple[T, R | Failed]]:
    """Run ``items`` one at a time in one process, so a crash or a hang points at its item."""
    executor = _healthy_executor()
    try:
        for item in items:
            future = executor.submit(_apply_batch, work, [item])
            try:
                (result,) = future.result(timeout=limits.item_timeout)
            except BrokenProcessPool:
                result = Failed("crashed", "the parser process crashed on this file")
            except FutureTimeoutError:
                _kill(executor)
                result = Failed("timeout", f"took longer than {limits.item_timeout:g} s")
            if isinstance(result, Failed) and result.reason != "error":
                executor.shutdown(wait=True, cancel_futures=True)
                executor = _healthy_executor()
            yield item, result
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
