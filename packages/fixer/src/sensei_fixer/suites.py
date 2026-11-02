"""Test runs before and after a fix (8.7): what passed and failed, read from what each framework
reports, and whether the fix made any test stop passing.

**A pass is the framework's report, never an exit code.** Code the fix changed runs inside the
test process: it can end that process with 0 (``os._exit(0)``) after its tests failed. So a run
passes only when it exited 0 *and* its report (the summary each framework prints last) says
tests passed and none failed; a run that says nothing (killed before its summary) passes nothing.
(Code that prints a fake report is the anti-gaming checks' to block: :mod:`.antigaming`.)

A suite often has failures of its own in the sandbox (a test that needs a service, a key, the
network): the fix is held to the suite **as it was** (its baseline, run at the commit being
fixed), never to a clean run it never had. A fix passes the suite when:

- every test that passed before still passes (pytest names them: ``-rA``'s ``PASSED`` lines;
  vitest and jest are held to their counts): none newly failing, skipped or deselected;
- as many tests pass as before, plus the reproducing test's;
- no test fails that didn't fail before, and, when the suite failed without naming its
  failures (a crash, coverage under its floor), it did so before too.

What each framework prints:

- pytest (run with ``-rA``): its short summary's ``PASSED``/``FAILED``/``ERROR <id>`` lines, and
  the counts on its last line (``2 failed, 40 passed in 3.1s``);
- vitest: its ``FAIL  <file> > <test>`` lines, and ``Tests  2 failed | 40 passed (42)``;
- jest: its ``FAIL <file>`` lines and each failure's ``● <test>`` heading, and
  ``Tests:       2 failed, 40 passed, 42 total``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sensei_fixer.frameworks import Framework
from sensei_fixer.runs import RunResult

MAX_LISTED = 20  # tests named in what's said
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", re.MULTILINE)
_PYTEST_PASSED = re.compile(r"^PASSED (.+?)\s*$", re.MULTILINE)
_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")
_PYTEST_SUMMARY = re.compile(r"^=*\s*(?:no tests ran|\d+ \w+.*) in [\d.]+s", re.MULTILINE)
_VITEST_FAILED = re.compile(r"^\s*FAIL\s+(\S.*?)\s*$", re.MULTILINE)
_VITEST_COUNTS = re.compile(r"^\s*Tests\s+(.+?)\s*\((\d+)\)\s*$", re.MULTILINE)
_JEST_FILE = re.compile(r"^\s*FAIL\s+(\S+)", re.MULTILINE)
_JEST_TEST = re.compile(r"^\s*● (.+?)\s*$", re.MULTILINE)
_JEST_COUNTS = re.compile(r"^Tests:\s+(.+?),\s*(\d+) total", re.MULTILINE)
_JEST_NOT_A_TEST = re.compile(r"^(Test suite failed to run|Console)\b")
_COUNT = re.compile(r"(\d+) (\w+)")
_PY_TEST = re.compile(r"^\s*(?:async\s+)?def (test\w*)\s*\(", re.MULTILINE)
_JS_TEST = re.compile(r"(?<![\w.])(?:it|test)\s*\(")


@dataclass(frozen=True, slots=True)
class Report:
    """What a framework's summary says."""

    passed: int
    failed: int  # failures and errors
    total: int  # what ran (skips included where the framework counts them)
    failing: frozenset[str]  # the failing tests (or files) it names
    passing: frozenset[str]  # the passing tests it names (pytest's -rA; empty for the others)


def _pytest(output: str) -> Report | None:
    summary = _PYTEST_SUMMARY.findall(output)
    if not summary:
        return None
    counts: dict[str, int] = {}
    for n, kind in _PYTEST_COUNTS.findall(summary[-1]):
        counts["error" if kind.startswith("error") else kind] = int(n)
    failed = counts.get("failed", 0) + counts.get("error", 0)
    total = sum(counts.get(k, 0) for k in ("passed", "failed", "error", "xfailed", "xpassed"))
    return Report(counts.get("passed", 0), failed, total,
                  frozenset(_PYTEST_FAILED.findall(output)),
                  frozenset(_PYTEST_PASSED.findall(output)))  # fmt: skip


def _counted(words: str) -> dict[str, int]:
    return {kind: int(n) for n, kind in _COUNT.findall(words)}


def _vitest(output: str) -> Report | None:
    found = _VITEST_COUNTS.findall(output)
    if not found:
        return None
    words, total = found[-1]
    counts = _counted(words)
    return Report(counts.get("passed", 0), counts.get("failed", 0), int(total),
                  frozenset(_VITEST_FAILED.findall(output)), frozenset())  # fmt: skip


def _jest(output: str) -> Report | None:
    found = _JEST_COUNTS.findall(output)
    if not found:
        return None
    words, total = found[-1]
    counts = _counted(words)
    tests = {t for t in _JEST_TEST.findall(output) if not _JEST_NOT_A_TEST.match(t)}
    return Report(counts.get("passed", 0), counts.get("failed", 0), int(total),
                  frozenset({*_JEST_FILE.findall(output), *tests}), frozenset())  # fmt: skip


_READERS = {"pytest": _pytest, "vitest": _vitest, "jest": _jest}


def read_report(framework: Framework, result: RunResult) -> Report | None:
    """What ``result``'s framework reported (None: it printed no summary)."""
    return _READERS[framework](result.full or result.output)


def passed(framework: Framework, result: RunResult, *, at_least: int = 1) -> bool:
    """``result`` passed by its report: it exited 0, and its summary says ``at_least`` tests
    passed and none failed (an exit code alone proves nothing: see the module)."""
    if not result.passed or result.timed_out:
        return False
    report = read_report(framework, result)
    return report is not None and report.failed == 0 and report.passed >= max(1, at_least)


def count_tests(framework: Framework, content: str) -> int:
    """How many tests a test file holds (at least one: what its report must show passing)."""
    found = _PY_TEST.findall(content) if framework == "pytest" else _JS_TEST.findall(content)
    return max(1, len(found))


@dataclass(frozen=True, slots=True)
class SuiteRun:
    """What one run of the whole suite reported."""

    ok: bool  # it passed by its report (exit 0, nothing failed)
    exit_code: int | None
    failed: frozenset[str]  # the failing tests (or files) it named
    total: int | None  # how many tests ran (None: it didn't say)
    readable: bool  # it reported, and named its failures (if any)
    timed_out: bool = False
    tail: str = ""  # the end of its output, to show
    passed: int | None = None  # how many passed (None: it didn't say)
    passing: frozenset[str] = frozenset()  # which passed (pytest)

    def json(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "exit_code": self.exit_code, "failed": sorted(self.failed),
            "total": self.total, "readable": self.readable, "timed_out": self.timed_out,
            "tail": self.tail, "passed": self.passed, "passing": sorted(self.passing),
        }  # fmt: skip

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> SuiteRun:
        return cls(
            ok=bool(raw.get("ok")), exit_code=raw.get("exit_code"),
            failed=frozenset(str(f) for f in raw.get("failed") or ()), total=raw.get("total"),
            readable=bool(raw.get("readable")), timed_out=bool(raw.get("timed_out")),
            tail=str(raw.get("tail") or ""), passed=raw.get("passed"),
            passing=frozenset(str(p) for p in raw.get("passing") or ()),
        )  # fmt: skip


def read_suite(framework: Framework, result: RunResult) -> SuiteRun:
    """What ``result`` (a run of the whole suite) reported."""
    report = None if result.timed_out else read_report(framework, result)
    if report is None:
        return SuiteRun(
            ok=False, exit_code=result.exit_code, failed=frozenset(), total=None, readable=False,
            timed_out=result.timed_out, tail=result.output[-2_500:],
        )  # fmt: skip
    ok = result.passed and report.failed == 0
    # Its failures are all named (a count it can't account for may hide which ones).
    named = len(report.failing) >= report.failed
    return SuiteRun(
        ok=ok, exit_code=result.exit_code, failed=report.failing, total=report.total,
        readable=named, tail=result.output[-2_500:], passed=report.passed,
        passing=report.passing,
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class SuiteVerdict:
    passed: bool
    reason: str  # why not (or, when it passed, what is left as it was)
    new_failures: tuple[str, ...] = ()
    known_failures: tuple[str, ...] = ()  # failing before the fix too


def _listed(names: frozenset[str] | set[str]) -> str:
    shown = sorted(names)[:MAX_LISTED]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _lost(baseline: SuiteRun, after: SuiteRun, repro_tests: int) -> str | None:
    """Why fewer tests pass with the fix than before it (None: they don't)."""
    if after.total is not None and baseline.total is not None and after.total < baseline.total:
        return f"Fewer tests ran with the fix ({after.total}) than before it ({baseline.total})."
    if baseline.passing:
        gone = baseline.passing - after.passing
        if gone:
            return f"Tests that passed before don't pass with the fix: {_listed(gone)}."
    if baseline.passed is not None and after.passed is not None:
        others = after.passed - repro_tests
        if others < baseline.passed:
            return (f"Fewer tests pass with the fix ({others}, besides the reproducing test's "
                    f"{repro_tests}) than before it ({baseline.passed}).")  # fmt: skip
    return None


def compare(  # noqa: PLR0911 - a verdict per case
    baseline: SuiteRun, after: SuiteRun, *, repro_tests: int = 1
) -> SuiteVerdict:
    """Whether ``after`` (the suite with the fix, and its reproducing test of ``repro_tests``
    tests) passes against ``baseline`` (the suite before it)."""
    known = tuple(sorted(after.failed & baseline.failed))
    if after.timed_out:
        return SuiteVerdict(False, "The suite didn't finish in time with the fix.")
    if after.passed is None:
        return SuiteVerdict(
            False, f"The suite (exit {after.exit_code}) printed no report of what passed."
        )
    lost = _lost(baseline, after, repro_tests)
    if lost:
        return SuiteVerdict(False, lost)
    if after.ok:
        return SuiteVerdict(True, "The whole suite passes.")
    if not after.readable:
        return SuiteVerdict(
            False, f"The suite failed (exit {after.exit_code}) and its failures couldn't be read."
        )
    new = after.failed - baseline.failed
    if new:
        return SuiteVerdict(False, f"Tests that passed before fail with the fix: {_listed(new)}.",
                            new_failures=tuple(sorted(new)), known_failures=known)  # fmt: skip
    if not baseline.readable and not baseline.ok:
        return SuiteVerdict(False, "The suite's failures before the fix couldn't all be read.")
    if after.failed:
        return SuiteVerdict(
            True,
            f"No new failures; {len(after.failed)} failed before the fix too: "
            f"{_listed(after.failed)}.",
            known_failures=known,
        )
    if baseline.ok:
        return SuiteVerdict(False, f"The suite now exits {after.exit_code}, naming no failures.")
    return SuiteVerdict(
        True, f"No failures named; the suite exits {after.exit_code} before the fix too."
    )
