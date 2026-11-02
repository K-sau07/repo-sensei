"""The whole suite before and after a fix (8.7): failures read from real pytest output and from
vitest's and jest's, and the fix held to the suite as it was (its baseline)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sensei_fixer import frameworks
from sensei_fixer.runs import RunResult
from sensei_fixer.suites import SuiteRun, compare, read_suite

VITEST = """
 \u276f tests/sum.test.ts (3 tests | 1 failed) 5ms
   \u00d7 sum > adds 3ms
     \u2192 expected 3 to be 4

\u23af\u23af\u23af\u23af\u23af\u23af\u23af Failed Tests 1 \u23af\u23af\u23af\u23af\u23af\u23af\u23af

 FAIL  tests/sum.test.ts > sum > adds
AssertionError: expected 3 to be 4

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 2 passed (3)
   Duration  300ms
"""
JEST = """
FAIL src/sum.test.js
  sum
    \u2715 adds (3 ms)
    \u2713 subtracts

  ● sum \u203a adds

    expect(received).toBe(expected)

Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 3 passed, 4 total
"""


def _pytest_run(tmp_path: Path, tests: str) -> RunResult:
    (tmp_path / "test_x.py").write_text(tests)
    setup = frameworks.TestSetup("pytest", "", "", "python")
    argv = [sys.executable, *setup.suite()[1:]]
    done = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, check=False)  # noqa: S603
    out = done.stdout + done.stderr
    return RunResult(done.returncode == 0, done.returncode, out[-6000:], 1.0, full=out)


def test_pytests_failures_are_read_from_its_summary(tmp_path: Path) -> None:
    run = _pytest_run(tmp_path, (
        "import pytest\n\n\ndef test_ok():\n    pass\n\n\ndef test_bad():\n    assert 1 == 2\n\n\n"
        "@pytest.mark.parametrize('n', [1, 2])\ndef test_param(n):\n    assert n == 1\n\n\n"
        "@pytest.fixture\ndef broken():\n    raise RuntimeError\n\n\n"
        "def test_errors(broken):\n    pass\n"
    ))  # fmt: skip
    found = read_suite("pytest", run)
    assert not found.ok
    assert found.readable
    assert found.failed == {
        "test_x.py::test_bad", "test_x.py::test_param[2]", "test_x.py::test_errors",
    }  # fmt: skip
    assert found.total == 5


def test_vitests_and_jests_failures_are_read() -> None:
    vitest = read_suite("vitest", RunResult(False, 1, VITEST, 1.0))
    assert (vitest.failed, vitest.total, vitest.readable) == (
        {"tests/sum.test.ts > sum > adds"}, 3, True,
    )  # fmt: skip
    jest = read_suite("jest", RunResult(False, 1, JEST, 1.0))
    assert jest.failed == {"src/sum.test.js", "sum \u203a adds"}
    assert (jest.total, jest.readable) == (4, True)
    crashed = read_suite("jest", RunResult(False, 1, "Tests:       2 failed, 2 total", 1.0))
    assert not crashed.readable  # it failed, naming nothing
    passed = read_suite("vitest", RunResult(True, 0, "Tests  3 passed (3)", 1.0))
    assert passed.ok
    assert passed.failed == frozenset()
    assert passed.total == 3


def _suite(failed: set[str] | None = None, total: int = 10, *, exit_code: int | None = None,
           readable: bool = True) -> SuiteRun:  # fmt: skip
    failed = failed or set()
    code = exit_code if exit_code is not None else (1 if failed else 0)
    return SuiteRun(ok=code == 0, exit_code=code, failed=frozenset(failed), total=total,
                    readable=readable)  # fmt: skip


def test_a_fix_is_held_to_the_suite_as_it_was() -> None:
    clean, known = _suite(), _suite({"t::needs_db"})
    assert compare(clean, _suite(total=11)).passed
    held = compare(known, _suite({"t::needs_db"}, total=11))
    assert held.passed
    assert held.known_failures == ("t::needs_db",)
    broke = compare(known, _suite({"t::needs_db", "t::totals"}, total=11))
    assert not broke.passed
    assert broke.new_failures == ("t::totals",)
    fewer = compare(clean, _suite(total=7))
    assert not fewer.passed
    assert "Fewer tests ran" in fewer.reason
    unread = compare(clean, _suite(exit_code=1, readable=False))
    assert not unread.passed
    assert "couldn't be read" in unread.reason
    timed_out = SuiteRun(False, None, frozenset(), None, readable=False, timed_out=True)
    assert not compare(clean, timed_out).passed
    # Exiting non-zero with no failures named: fine only when it did so before the fix too.
    floor = _suite(exit_code=2)
    assert compare(floor, _suite(exit_code=2)).passed
    assert not compare(clean, _suite(exit_code=2)).passed


def test_a_baseline_round_trips_through_the_run() -> None:
    known = _suite({"t::needs_db"}, total=12)
    assert SuiteRun.from_json(known.json()) == known
