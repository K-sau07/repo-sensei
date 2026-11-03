"""The whole suite before and after a fix (8.7): failures read from real pytest output and from
vitest's and jest's, and the fix held to the suite as it was (its baseline)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sensei_fixer import frameworks
from sensei_fixer.runs import RunResult
from sensei_fixer.suites import SuiteRun, compare, count_tests, passed, read_suite

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
           readable: bool = True, passed: int | None = None,
           passing: set[str] | None = None) -> SuiteRun:  # fmt: skip
    failed = failed or set()
    code = exit_code if exit_code is not None else (1 if failed else 0)
    return SuiteRun(
        ok=code == 0 and not failed, exit_code=code, failed=frozenset(failed), total=total,
        readable=readable, passed=total - len(failed) if passed is None else passed,
        passing=frozenset(passing or ()),
    )  # fmt: skip


def test_a_fix_is_held_to_the_suite_as_it_was() -> None:
    # The run after a fix has the reproducing test's one test too.
    clean, known = _suite(), _suite({"t::needs_db"})
    assert compare(clean, _suite(total=11)).passed
    held = compare(known, _suite({"t::needs_db"}, total=11))
    assert held.passed
    assert held.known_failures == ("t::needs_db",)
    broke = compare(known, _suite({"t::needs_db", "t::totals"}, total=12, passed=10))
    assert not broke.passed
    assert broke.new_failures == ("t::totals",)
    fewer = compare(clean, _suite(total=7))
    assert not fewer.passed
    assert "Fewer tests ran" in fewer.reason
    unread = compare(clean, _suite(total=11, exit_code=1, readable=False))
    assert not unread.passed
    assert "couldn't be read" in unread.reason
    timed_out = SuiteRun(False, None, frozenset(), None, readable=False, timed_out=True)
    assert not compare(clean, timed_out).passed
    # Exiting non-zero with no failures named: fine only when it did so before the fix too.
    floor = _suite(exit_code=2)
    assert compare(floor, _suite(total=11, exit_code=2)).passed
    assert not compare(clean, _suite(total=11, exit_code=2)).passed


def test_a_test_that_stops_passing_is_lost_however_it_goes() -> None:
    # A skip (vitest, jest: counted, not named) leaves the total as it was: fewer pass.
    skipped = compare(_suite(total=10), _suite(total=11, passed=10), repro_tests=1)
    assert not skipped.passed
    assert skipped.reason.startswith("Fewer tests pass with the fix (9")
    # pytest names what passed: one skipped or deselected is one gone, whatever the counts say.
    before = _suite(total=3, passing={"t::a", "t::b", "t::c"})
    after = _suite(total=4, passing={"t::a", "t::c", "t::repro", "t::repro2"})
    gone = compare(before, after, repro_tests=1)
    assert not gone.passed
    assert gone.reason == "Tests that passed before don't pass with the fix: t::b."
    # A run that printed no report proves nothing, whatever its exit code.
    silent = SuiteRun(True, 0, frozenset(), None, readable=False)
    assert (
        compare(_suite(), silent).reason == "The suite (exit 0) printed no report of what passed."
    )


def test_a_pass_is_the_report_not_the_exit_code() -> None:
    failing = "FAILED t.py::test_a - ZeroDivisionError\n==== 1 failed, 2 passed in 0.10s ===="
    forced = RunResult(True, 0, failing, 1.0)  # os._exit(0) after the summary
    assert not passed("pytest", forced)
    assert not read_suite("pytest", forced).ok
    assert read_suite("pytest", forced).failed == {"t.py::test_a"}
    killed = RunResult(True, 0, "collecting ...", 1.0)  # os._exit(0) before any summary
    assert not passed("pytest", killed)
    assert not read_suite("pytest", killed).readable
    fine = RunResult(True, 0, "==== 2 passed in 0.10s ====", 1.0)
    assert passed("pytest", fine)
    assert not passed("pytest", fine, at_least=3)  # the reproducing file holds three tests
    assert passed("vitest", RunResult(True, 0, "      Tests  2 passed (2)", 1.0))
    assert not passed("jest", RunResult(True, 0, "Tests:       1 failed, 1 passed, 2 total", 1.0))
    assert count_tests("pytest", "def test_a():\n    pass\n\nasync def test_b():\n    pass\n") == 2
    assert count_tests("vitest", "it('a', () => {})\ntest('b', () => {})\nit.skip('c')") == 2


def test_a_baseline_round_trips_through_the_run() -> None:
    known = _suite({"t::needs_db"}, total=12, passing={"t::a"})
    assert SuiteRun.from_json(known.json()) == known


def test_a_file_that_couldnt_run_is_one_error_however_often_it_is_printed() -> None:
    broken = (
        "FAIL src/broken.test.js\n  ● Test suite failed to run\n\n    Cannot find module './gone'\n"
    )
    summary = "\nSummary of all failing tests\n" + broken
    jest = (
        broken
        + "PASS src/ok.test.js\n"
        + summary
        + ("\nTest Suites: 1 failed, 1 passed, 2 total\nTests:       3 passed, 3 total\n")
    )
    run = read_suite("jest", RunResult(False, 1, jest, 1.0))
    assert (run.readable, run.failed) == (True, frozenset({"src/broken.test.js"}))
    load = "Error: Failed to load url ./gone (resolved id: ./gone)"
    vitest = (f" FAIL  tests/broken.test.ts [ tests/broken.test.ts ]\n{load}\n\n{load}\n"
              " Test Files  1 failed | 1 passed (2)\n      Tests  3 passed (3)\n")  # fmt: skip
    found = read_suite("vitest", RunResult(False, 1, vitest, 1.0))
    assert found.readable


def test_a_new_test_failing_only_on_whats_missing_proves_nothing() -> None:
    from sensei_fixer.suites import without_change  # noqa: PLC0415

    def ran(error: str) -> RunResult:
        out = (f"FAILED tests/test_m.py::test_m - {error}\n"
               "==== 1 failed in 0.1s ====")  # fmt: skip
        return RunResult(False, 1, out, 0.1, full=out)

    assert without_change("pytest", ran("AttributeError: module has no 'median'")) == "inconclusive"
    assert without_change("pytest", ran("assert 2.0 == 2.5")) == "fails"
    assert without_change("pytest", ran("ZeroDivisionError: division by zero")) == "fails"


def test_the_suite_runs_as_the_project_configures_it() -> None:
    # No option leaves the new tests out (Jest's would replace the project's own ignores):
    # they're moved aside while it runs.
    for framework in ("pytest", "vitest", "jest"):
        argv = frameworks.TestSetup(framework, "", "", "python").suite()  # type: ignore[arg-type]
        assert not any("ignore" in a.lower() or a == "--exclude" for a in argv), argv
