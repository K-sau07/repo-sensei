"""The parse stage: the process pool (batches, errors, crashes) and parsing a working copy."""

from __future__ import annotations

import os
import signal
import tarfile
import time
from pathlib import Path
from typing import Any

import billiard  # pyright: ignore[reportMissingTypeStubs]
import pytest

from sensei_engine.parse.parser import ParseStatus
from sensei_engine.parse.pool import Failed, run_parallel
from sensei_engine.parse.stage import (
    FileParse,
    ParseReport,
    ParseRequest,
    parse_files,
    parse_request,
    read_source,
)
from sensei_testing.pool_work import nap, pool_in_daemon, pool_then_wait, shout

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"


def test_every_item_comes_back_once_across_batches() -> None:
    items = [f"item{i}" for i in range(23)]
    results = dict(run_parallel(shout, items, workers=2, batch_size=4))
    assert results == {item: item.upper() for item in items}


def test_an_exception_fails_only_its_item() -> None:
    results = dict(run_parallel(shout, ["a", "raise", "b"], workers=1, batch_size=8))
    assert results["a"] == "A"
    assert results["b"] == "B"
    assert results["raise"] == Failed("error", "ValueError: boom")


def test_a_crash_fails_only_the_item_that_crashes() -> None:
    items = [f"ok{i}" for i in range(12)]
    items.insert(5, "crash")
    results = dict(run_parallel(shout, items, workers=2, batch_size=3))
    assert len(results) == 13
    failed = results.pop("crash")
    assert isinstance(failed, Failed)
    assert failed.reason == "crashed"
    assert results == {item: item.upper() for item in items if item != "crash"}


@pytest.mark.slow
def test_an_item_that_hangs_is_killed_and_the_rest_carry_on() -> None:
    # Nothing finishes for a second (the 30 s nap holds the only worker), so the pool is
    # killed; one at a time, the nap runs past its timeout and its process is killed too.
    items = [0.0, 30.0, 0.0]
    started = time.monotonic()
    results = dict(
        run_parallel(nap, items, workers=1, batch_size=3, item_timeout=0.5, stall_seconds=1.0)
    )
    assert time.monotonic() - started < 20
    assert results[30.0] == Failed("timeout", "took longer than 0.5 s")
    assert results[0.0] == 0.0


def test_in_process_mode_needs_no_pool() -> None:
    assert dict(run_parallel(shout, ["x"], workers=0)) == {"x": "X"}


@pytest.fixture(scope="module")
def worktree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("parse")
    with tarfile.open(FIXTURE) as archive:
        archive.extractall(root, filter="data")
    return root / "fastapi-template"


def test_paths_must_stay_inside_the_working_copy(worktree: Path, tmp_path: Path) -> None:
    main = worktree / "backend" / "app" / "main.py"
    assert read_source(worktree, "backend/app/main.py") == main.read_bytes()
    for path in ("../outside.py", "/etc/passwd", "backend/../../x.py"):
        with pytest.raises(PermissionError):
            read_source(worktree, path)
    (tmp_path / "secret.py").write_text("x = 1\n")
    linked = worktree / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    (worktree / "link.py").symlink_to(tmp_path / "secret.py")
    try:
        with pytest.raises(PermissionError):
            read_source(worktree, "linked/secret.py")
        # The file itself is opened without following links.
        with pytest.raises(OSError, match="symbolic links"):
            read_source(worktree, "link.py")
    finally:
        linked.unlink()
        (worktree / "link.py").unlink()


def test_an_unreadable_file_is_marked_not_raised(worktree: Path) -> None:
    result = parse_request(ParseRequest(str(worktree), "missing.py", "python"))
    assert (result.status, result.failure) == (ParseStatus.UNREADABLE, "FileNotFoundError")


def test_a_working_copy_parses_in_parallel(worktree: Path) -> None:
    files = sorted(
        (str(path.relative_to(worktree)), language)
        for path in worktree.rglob("*")
        for language in [{".py": "python", ".ts": "typescript", ".tsx": "tsx"}.get(path.suffix)]
        if language is not None and path.is_file() and not path.is_symlink()
    )
    files.append(("missing.py", "python"))
    report = ParseReport()
    seen: list[str] = []
    for parsed in parse_files(worktree, files, workers=2, batch_size=16):
        report.add(parsed)
        seen.append(parsed.path)
    assert sorted(seen) == sorted(path for path, _ in files)
    details = report.details()
    assert details["files"] == len(files) == 153
    assert details["parsed"] == 152
    assert details["with_syntax_errors"] == 0
    assert details["statuses"] == {"parsed": 152, "unreadable": 1}
    assert details["examples"] == {"unreadable": ["missing.py"]}
    assert details["captures"]["defs"] > 500


def test_the_report_adds_up_problems() -> None:
    report = ParseReport()
    report.add(FileParse("a.py", "python", ParseStatus.PARSED, error_count=3, seconds=0.5))
    report.add(FileParse("b.py", "python", ParseStatus.PARSED, captures={"defs": 2}))
    report.add(FileParse("c.ts", "typescript", ParseStatus.TIMED_OUT, seconds=2.0))
    report.add(FileParse("d.ts", "typescript", ParseStatus.FAILED, failure="crashed: x"))
    assert report.details() == {
        "files": 4,
        "parsed": 2,
        "with_syntax_errors": 1,
        "syntax_error_nodes": 3,
        "statuses": {"failed": 1, "parsed": 2, "timed_out": 1},
        "captures": {"defs": 2},
        "parse_seconds": 2.5,
        "examples": {"failed": ["d.ts"], "syntax_errors": ["a.py"], "timed_out": ["c.ts"]},
    }


# ---------------------------------------------------------------- inside a Celery worker


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("method", ["spawn", "fork"])
def test_the_pool_runs_inside_a_daemonic_celery_worker(method: str) -> None:
    context: Any = billiard.get_context(method)  # billiard has no type information
    queue = context.Queue()
    process = context.Process(target=pool_in_daemon, args=(queue,), daemon=True)
    process.start()
    try:
        result = queue.get(timeout=60)
    finally:
        process.join(10)
    assert result == [
        ("a", "'A'"),
        ("crash", "Failed(reason='crashed', message='the parser process crashed on this file')"),
    ]


@pytest.mark.slow
def test_workers_exit_when_their_parent_is_killed() -> None:
    context: Any = billiard.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=pool_then_wait, args=(queue,), daemon=True)
    process.start()
    workers: list[int] = queue.get(timeout=60)
    assert len(workers) == 2
    assert all(_alive(pid) for pid in workers)
    os.kill(process.pid, signal.SIGKILL)  # a hard time limit, or the out-of-memory killer
    process.join(10)
    deadline = time.monotonic() + 10
    while any(_alive(pid) for pid in workers) and time.monotonic() < deadline:
        time.sleep(0.2)
    leftover = [pid for pid in workers if _alive(pid)]
    for pid in leftover:
        os.kill(pid, signal.SIGKILL)
    assert leftover == []
