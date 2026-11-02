"""What a pipeline gets and gives back, and the bench's reference pipelines.

A pipeline gets what a user would give: the issue (and its stack trace) and the repository
with the bug in it. It answers with a diff it proposes, or says honestly why not (it couldn't
reproduce the bug, a rule blocked it, it failed): stopping is never unsafe, a wrong fix is.

- ``gold``: the bug's patch reversed (the bench's upper bound, and a check of the judge);
- ``noop``: proposes nothing (the lower bound).

The real fix pipeline (8.4-8.7) is added to :data:`PIPELINES` when it exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from sensei_evals.fixbench.bench import Bench
from sensei_evals.fixbench.workspace import Workspace

Outcome = Literal["proposed", "not_reproduced", "blocked", "failed"]


@dataclass(frozen=True, slots=True)
class FixTask:
    bug_id: str  # to log by; a real pipeline mustn't look anything up with it
    checkout: Path  # the app with the bug in it, a git repository (the pipeline may write here)
    issue: str
    stack_trace: str | None


@dataclass(frozen=True, slots=True)
class FixAttempt:
    outcome: Outcome
    diff: str = ""  # proposed: the change, against the checkout's HEAD
    reason: str = ""
    cost_usd: Decimal = Decimal(0)


class FixPipeline(Protocol):
    name: str
    # Its diffs are the bench's own: they may be judged on this machine (else only in a sandbox).
    trusted: bool

    async def fix(self, task: FixTask) -> FixAttempt: ...


@dataclass(slots=True)
class GoldPipeline:
    bench: Bench
    name: str = "gold"
    trusted: bool = True

    async def fix(self, task: FixTask) -> FixAttempt:
        bug = next(b for b in self.bench.bugs if b.id == task.bug_id)
        checkout = Workspace(task.checkout)
        head = checkout.git("rev-parse", "HEAD").strip()
        checkout.apply(bug.patch, reverse=True)
        return FixAttempt("proposed", diff=checkout.diff(head), reason="the gold fix")


@dataclass(slots=True)
class NoopPipeline:
    name: str = "noop"
    trusted: bool = True

    async def fix(self, task: FixTask) -> FixAttempt:
        return FixAttempt("failed", reason="does nothing")


PIPELINES: dict[str, Callable[[Bench], FixPipeline]] = {
    "gold": GoldPipeline,
    "noop": lambda _bench: NoopPipeline(),
}
