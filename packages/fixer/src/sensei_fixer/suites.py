"""A repository's whole test suite, run before and after a fix (8.7): which tests failed, read
from the run's output, and whether the fix made any test fail that passed before.

A suite often has failures of its own in the sandbox (a test that needs a service, a key, the
network): the fix is held to the suite **as it was** (its baseline, run at the commit being
fixed), never to a clean run it never had. A fix passes the suite when no test fails that
didn't fail before, no fewer tests run, and, when the suite failed without naming its failures
(a crash, coverage under its floor), it did so before too. A run whose failures can't be read
passes nothing.

What a run reports is read from what each framework prints:

- pytest (run with ``-rfE``): its short summary's ``FAILED <id>`` and ``ERROR <id>`` lines, and
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

MAX_LISTED = 20  # failing tests named in what's said and kept
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", re.MULTILINE)
_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")
_PYTEST_SUMMARY = re.compile(r"^=*\s*(?:no tests ran|\d+ \w+.*) in [\d.]+s", re.MULTILINE)
_VITEST_FAILED = re.compile(r"^\s*FAIL\s+(\S.*?)\s*$", re.MULTILINE)
_VITEST_COUNTS = re.compile(r"^\s*Tests\s+(.+?)\s*\((\d+)\)\s*$", re.MULTILINE)
_JEST_FILE = re.compile(r"^\s*FAIL\s+(\S+)", re.MULTILINE)
_JEST_TEST = re.compile(r"^\s*● (.+?)\s*$", re.MULTILINE)
_JEST_COUNTS = re.compile(r"^Tests:\s+(.+?),\s*(\d+) total", re.MULTILINE)
_JEST_NOT_A_TEST = re.compile(r"^(Test suite failed to run|Console)\b")
_COUNT = re.compile(r"(\d+) (\w+)")


@dataclass(frozen=True, slots=True)
class SuiteRun:
    """What one run of the suite reported."""

    ok: bool  # it exited 0
    exit_code: int | None
    failed: frozenset[str]  # the failing tests (or files) it named
    total: int | None  # how many tests ran (None: it didn't say)
    readable: bool  # its failures (if any) were read from it
    timed_out: bool = False
    tail: str = ""  # the end of its output, to show

    def json(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "exit_code": self.exit_code, "failed": sorted(self.failed),
            "total": self.total, "readable": self.readable, "timed_out": self.timed_out,
            "tail": self.tail,
        }  # fmt: skip

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> SuiteRun:
        return cls(
            ok=bool(raw.get("ok")), exit_code=raw.get("exit_code"),
            failed=frozenset(str(f) for f in raw.get("failed") or ()), total=raw.get("total"),
            readable=bool(raw.get("readable")), timed_out=bool(raw.get("timed_out")),
            tail=str(raw.get("tail") or ""),
        )  # fmt: skip


def _pytest(output: str) -> tuple[frozenset[str], int | None, bool]:
    failed = frozenset(_PYTEST_FAILED.findall(output))
    summary = _PYTEST_SUMMARY.findall(output)
    if not summary:
        return failed, None, bool(failed)
    counts = {kind.rstrip("s") if kind.startswith("error") else kind: int(n)
              for n, kind in _PYTEST_COUNTS.findall(summary[-1])}  # fmt: skip
    ran = sum(counts.get(k, 0) for k in ("passed", "failed", "error", "xfailed", "xpassed"))
    named = counts.get("failed", 0) + counts.get("error", 0)
    return failed, ran, len(failed) >= named


def _vitest(output: str) -> tuple[frozenset[str], int | None, bool]:
    failed = frozenset(_VITEST_FAILED.findall(output))
    found = _VITEST_COUNTS.findall(output)
    if not found:
        return failed, None, bool(failed)
    words, total = found[-1]
    counts = {kind: int(n) for n, kind in _COUNT.findall(words)}
    return failed, int(total), bool(failed) or not counts.get("failed")


def _jest(output: str) -> tuple[frozenset[str], int | None, bool]:
    tests = {t for t in _JEST_TEST.findall(output) if not _JEST_NOT_A_TEST.match(t)}
    failed = frozenset({*_JEST_FILE.findall(output), *tests})
    found = _JEST_COUNTS.findall(output)
    if not found:
        return failed, None, bool(failed)
    words, total = found[-1]
    counts = {kind: int(n) for n, kind in _COUNT.findall(words)}
    return failed, int(total), bool(failed) or not counts.get("failed")


_READERS = {"pytest": _pytest, "vitest": _vitest, "jest": _jest}


def read_suite(framework: Framework, result: RunResult) -> SuiteRun:
    """What ``result`` (a run of the whole suite) reported."""
    output = result.full or result.output
    failed, total, readable = _READERS[framework](output)
    if result.passed:
        failed, readable = frozenset[str](), True
    elif result.timed_out:
        readable = False
    return SuiteRun(
        ok=result.passed, exit_code=result.exit_code, failed=failed, total=total,
        readable=readable, timed_out=result.timed_out, tail=result.output[-2_500:],
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


def compare(baseline: SuiteRun, after: SuiteRun) -> SuiteVerdict:  # noqa: PLR0911 - a verdict per case
    """Whether ``after`` (the suite with the fix, and its reproducing test) passes against
    ``baseline`` (the suite before it)."""
    known = tuple(sorted(after.failed & baseline.failed))
    if after.timed_out:
        return SuiteVerdict(False, "The suite didn't finish in time with the fix.")
    if after.total is not None and baseline.total is not None and after.total < baseline.total:
        said = f"Fewer tests ran with the fix ({after.total}) than before it ({baseline.total})."
        return SuiteVerdict(False, said)
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
    if not baseline.readable and not baseline.ok and baseline.failed:
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
