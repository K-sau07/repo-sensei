"""Checking the bench itself: a bug only measures something if

- its patch applies to the clean app;
- its hidden test **passes** on the clean app and **fails** with the bug, for a reason (an
  assertion or the bug's error, not a test that can't even be collected);
- the app's own suite still passes with the bug (else the bug is no test of finding it);
- the judge's review finds nothing in the gold fix (else the judge is wrong).

And each layer's suite passes on the clean app.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from sensei_evals.fixbench.bench import Bench, Bug
from sensei_evals.fixbench.review import parse_diff, review
from sensei_evals.fixbench.run import existing_literals
from sensei_evals.fixbench.runners import TestRunner
from sensei_evals.fixbench.workspace import PatchError, Workspace, workspace

# The hidden test failed to run at all, rather than failing.
_BROKEN = re.compile(
    r"ImportError|ModuleNotFoundError|SyntaxError|collected 0 items|no tests ran|"
    r"No test files found|Cannot find module|Failed to load"
)


class BugCheck(BaseModel):
    id: str
    problems: list[str] = []
    warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.problems


class Validation(BaseModel):
    suites: dict[str, bool]  # each layer's suite, on the clean app
    suite_output: dict[str, str] = {}
    bugs: list[BugCheck]

    @property
    def ok(self) -> bool:
        return all(self.suites.values()) and all(b.ok for b in self.bugs)


def _gold_diff(bug: Bug, planted: Workspace) -> str:
    head = planted.git("rev-parse", "HEAD").strip()
    planted.apply(bug.patch, reverse=True)
    diff = planted.diff(head)
    planted.git("reset", "-q", "--hard", head)
    return diff


async def check_bug(
    bench: Bench, bug: Bug, runner: TestRunner, *, suite_passes: bool = True
) -> BugCheck:
    check = BugCheck(id=bug.id)
    hidden = bug.spec.hidden_test
    with workspace(bench.app_dir) as ws:
        ws.write(hidden.path, bug.hidden_source)
        clean = await runner.run(ws.path, hidden)
        if not clean.passed:
            check.problems.append(f"the hidden test fails on the clean app:\n{clean.output}")
        try:
            ws.apply(bug.patch)
        except PatchError as error:
            check.problems.append(f"the patch doesn't apply: {error}")
            return check
        planted = await runner.run(ws.path, hidden)
        if planted.passed:
            check.problems.append("the hidden test passes with the bug in")
        elif _BROKEN.search(planted.output):
            check.problems.append(f"the hidden test breaks rather than fails:\n{planted.output}")
        (ws.path / hidden.path).unlink()
        suite = await runner.run(ws.path, bench.spec.suites[bug.spec.layer])
        if not suite.passed and suite_passes:  # (if it fails on the clean app, that's said)
            check.warnings.append("the app's own tests already catch it")
        ws.commit("bug")
        gold = _gold_diff(bug, ws)
        found = review(gold, bug.hidden_source, existing_literals(ws.path, parse_diff(gold)))
        check.problems.extend(f"the judge flags the gold fix: {f.rule}: {f.detail}" for f in found)
    return check


async def validate_bench(bench: Bench, bugs: list[Bug], runner: TestRunner) -> Validation:
    suites: dict[str, bool] = {}
    output: dict[str, str] = {}
    for layer, command in bench.spec.suites.items():
        with workspace(bench.app_dir) as ws:
            result = await runner.run(ws.path, command)
        suites[layer] = result.passed
        if not result.passed:
            output[layer] = result.output
    checks = [
        await check_bug(bench, bug, runner, suite_passes=suites[bug.spec.layer]) for bug in bugs
    ]
    return Validation(suites=suites, suite_output=output, bugs=checks)
