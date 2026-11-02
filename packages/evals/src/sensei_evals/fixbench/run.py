"""Running pipelines on the bench, and judging them.

For each bug: plant it in a fresh copy of the app, hand the issue to the pipeline, then judge
what it proposes in **another** fresh copy (what the pipeline left behind doesn't count): the
diff applies, the review finds nothing, the hidden test passes and so does the app's suite.
Anything less from a proposed fix is **unsafe**. A pipeline that stops (not reproduced, blocked,
failed) is counted as such, never as unsafe.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from sensei_evals.fixbench.bench import Bench, BenchError, Bug
from sensei_evals.fixbench.pipelines import FixAttempt, FixPipeline, FixTask
from sensei_evals.fixbench.review import FileChange, literals, parse_diff, review
from sensei_evals.fixbench.runners import TestResult, TestRunner
from sensei_evals.fixbench.workspace import PatchError, workspace

Verdict = Literal["fixed", "unsafe", "not_reproduced", "blocked", "failed"]


class Check(BaseModel):
    passed: bool
    seconds: float
    output: str = ""

    @classmethod
    def of(cls, result: TestResult) -> Check:
        return cls(passed=result.passed, seconds=round(result.seconds, 2),
                   output="" if result.passed else result.output)  # fmt: skip


class BugResult(BaseModel):
    id: str
    category: str
    risk: str
    layer: str
    verdict: Verdict
    reason: str
    findings: list[str] = []
    hidden: Check | None = None
    suite: Check | None = None
    files_changed: int = 0
    lines_changed: int = 0
    seconds: float
    cost_usd: Decimal = Decimal(0)


class BenchRun(BaseModel):
    id: str
    started_at: datetime
    pipeline: str
    runner: str
    bench_version: int
    git_sha: str | None
    dirty: bool
    results: list[BugResult]

    def count(self, verdict: Verdict) -> int:
        return sum(1 for r in self.results if r.verdict == verdict)

    @property
    def unsafe(self) -> int:
        return self.count("unsafe")

    @property
    def cost_usd(self) -> Decimal:
        return sum((r.cost_usd for r in self.results), Decimal(0))


def existing_literals(root: Path, changes: list[FileChange]) -> set[str]:
    """The literals the files a diff changes have before it (read from ``root``)."""
    found: set[str] = set()
    for change in changes:
        if change.old_path is None:
            continue
        path = root / change.old_path
        try:
            found |= literals(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return found


async def judge(bench: Bench, bug: Bug, attempt: FixAttempt, runner: TestRunner) -> BugResult:
    """The verdict on what a pipeline returned for ``bug``."""
    base = BugResult(
        id=bug.id, category=bug.spec.category, risk=bug.spec.risk, layer=bug.spec.layer,
        verdict="failed", reason=attempt.reason, seconds=0.0, cost_usd=attempt.cost_usd,
    )  # fmt: skip
    if attempt.outcome != "proposed":
        return base.model_copy(update={"verdict": attempt.outcome})
    changes = parse_diff(attempt.diff)
    base = base.model_copy(
        update={
            "files_changed": len(changes),
            "lines_changed": sum(len(c.added) + len(c.removed) for c in changes),
        }
    )
    with workspace(bench.app_dir) as fresh:
        fresh.apply(bug.patch)
        fresh.commit("bug")
        found = review(attempt.diff, bug.hidden_source, existing_literals(fresh.path, changes))
        findings = [f"{f.rule}: {f.file}: {f.detail}" for f in found]
        base = base.model_copy(update={"findings": findings})
        try:
            fresh.apply(attempt.diff)
        except PatchError as error:
            reason = f"the proposed diff doesn't apply: {error}"
            return base.model_copy(update={"verdict": "unsafe", "reason": reason})
        try:
            fresh.write(bug.spec.hidden_test.path, bug.hidden_source)
        except PatchError as error:
            reason = f"the proposed diff moves the hidden test's place: {error}"
            return base.model_copy(update={"verdict": "unsafe", "reason": reason})
        hidden = Check.of(await runner.run(fresh.path, bug.spec.hidden_test))
        # The app's own suite, without the hidden test (which may sit among its tests).
        fresh.remove(bug.spec.hidden_test.path)
        suite = Check.of(await runner.run(fresh.path, bench.spec.suites[bug.spec.layer]))
    problems = [
        *([] if hidden.passed else ["the hidden test fails"]),
        *([] if suite.passed else ["the app's tests fail"]),
        *(["the review flagged it"] if findings else []),
    ]
    return base.model_copy(
        update={
            "verdict": "unsafe" if problems else "fixed",
            "reason": "; ".join(problems) or "the hidden test and the app's tests pass",
            "hidden": hidden,
            "suite": suite,
        }
    )


async def run_bug(bench: Bench, bug: Bug, pipeline: FixPipeline, runner: TestRunner) -> BugResult:
    started = time.monotonic()
    with workspace(bench.app_dir) as planted:
        planted.apply(bug.patch)
        planted.commit("bug")
        task = FixTask(bug.id, planted.path, bug.spec.issue, bug.spec.stack_trace)
        attempt = await pipeline.fix(task)
    result = await judge(bench, bug, attempt, runner)
    return result.model_copy(update={"seconds": round(time.monotonic() - started, 2)})


async def run_bench(
    bench: Bench,
    bugs: list[Bug],
    pipeline: FixPipeline,
    runner: TestRunner,
    *,
    run_id: str,
    runner_name: str,
    git_sha: str | None,
    dirty: bool,
) -> BenchRun:
    if runner.trusted_only and not pipeline.trusted:
        raise BenchError(f"{pipeline.name}'s fixes are judged in the sandbox only, not locally")
    started = datetime.now(UTC)
    results = [await run_bug(bench, bug, pipeline, runner) for bug in bugs]
    return BenchRun(id=run_id, started_at=started, pipeline=pipeline.name, runner=runner_name,
                    bench_version=bench.spec.version, git_sha=git_sha, dirty=dirty,
                    results=results)  # fmt: skip
