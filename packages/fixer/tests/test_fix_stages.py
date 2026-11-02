"""Root cause, plan, patch and verify (8.7) on recorded answers, with real git and real pytest
runs: a plan checked before it counts; a patch accepted, retried with its failure fed back, or
blocked by an anti-gaming rule; verification from a clean checkout, the verifier's verdict,
risk and merge readiness."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from sensei_core.fixes.tickets import parse_ticket
from sensei_core.models import FixRisk, FixStage, FixStatus, ReproResult
from sensei_engine.agent.tools import ToolContext
from sensei_fixer import frameworks
from sensei_fixer.history import GitHistory
from sensei_fixer.patch import MAX_ATTEMPTS, patch_stage
from sensei_fixer.plan import plan_stage
from sensei_fixer.rootcause import root_cause_stage
from sensei_fixer.runs import FixRunner, RunResult
from sensei_fixer.stages import Advance, StageContext, Step, Stop
from sensei_fixer.verify import verify_stage
from sensei_testing.anthropic import FakeAnthropic, message, sse

TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/app/stats.py", line 2, in mean\n'
    "    return sum(values) / len(values)\n"
    "ZeroDivisionError: division by zero\n"
)
BAD = "def mean(values):\n    return sum(values) / len(values)\n"
FIXED_BODY = "    if not values:\n        return 0\n    return sum(values) / len(values)\n"
REPRO_PATH = "tests/test_sensei_repro_0123456789ab.py"
REPRO = "from app.stats import mean\n\n\ndef test_empty():\n    assert mean([]) == 0\n"
FILES = {
    "pyproject.toml": "[project]\nname = 'stats'\n",
    "conftest.py": "",
    "app/__init__.py": "",
    "app/stats.py": BAD,
    "tests/test_ok.py": "from app.stats import mean\n\n\ndef test_two():\n"
    "    assert mean([1, 3]) == 2\n",
}
FIX_ID = uuid.UUID("0199f000-0000-7000-8000-000000000abc")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for path, text in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    return root


@dataclass
class LocalPytest:
    """Our own tests' pytest, on this machine (the files run are this test's: ours)."""

    root: Path
    project: str

    async def run(self, argv: Sequence[str]) -> RunResult:
        cmd = [sys.executable if argv[0] == "python" else argv[0], *argv[1:]]
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.root / self.project, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )  # fmt: skip
        out, _ = await process.communicate()
        text, code = out.decode(), process.returncode
        return RunResult(code == 0, code, text[-6000:], time.monotonic() - started, full=text)


def _tools(root: Path) -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]  # the tools these runs call read files only
        org_id=None,  # type: ignore[arg-type]
        repo_id=None,  # type: ignore[arg-type]
        snapshot_id=None,  # type: ignore[arg-type]
        repo="o/stats", sha="b" * 40, root=root, tree=sorted(FILES),
    )  # fmt: skip


def _tool(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def _fake(*turns: list[dict[str, Any]]) -> FakeAnthropic:
    queue: list[dict[str, Any]] = []
    for content in turns:
        found = message("", stop_reason="tool_use")
        found["content"] = content
        queue.append(found)

    def reply(_request: httpx2.Request) -> httpx2.Response:
        return sse(queue.pop(0))

    return FakeAnthropic(routes={"/v1/messages": reply})


ROOT_CAUSE: dict[str, Any] = {"claim": "mean() divides by len(values), zero for an empty list",
              "path": "app/stats.py", "start_line": 1, "end_line": 2, "hypotheses": [],
              "areas": {}}  # fmt: skip
REPRO_FACT = {
    "framework": "pytest", "project": "", "test_path": REPRO_PATH, "test": REPRO,
    "runs": [{"passed": False, "output": "app/stats.py:2: ZeroDivisionError"}] * 3,
}  # fmt: skip
PLAN: dict[str, Any] = {"summary": "Return 0 for an empty list.",
        "steps": [{"description": "Guard the empty list", "path": "app/stats.py"}],
        "files": [{"path": "app/stats.py", "change": "modify", "why": "the division"}],
        "risks": [], "risk": {"level": "low", "reasons": []}}  # fmt: skip


def _stage(stage: FixStage, facts: dict[str, Any], steps: list[Step] | None = None) -> StageContext:
    async def record(step: Step) -> None:
        if steps is not None:
            steps.append(step)

    async def spend(_t: int, _c: Decimal, _s: float) -> None:
        return None

    async def check() -> None:
        return None

    return StageContext(
        fix_id=FIX_ID, org_id=None, repo_id=None, stage=stage,  # type: ignore[arg-type]
        ticket=parse_ticket(TRACE, paths=["app/stats.py"]), facts=facts, record=record,
        spend=spend, check=check,
    )  # fmt: skip


def _workspace(root: Path) -> Any:
    @asynccontextmanager
    async def workspace(_ctx: StageContext) -> AsyncGenerator[tuple[ToolContext, GitHistory]]:
        yield _tools(root), GitHistory(root, _head(root))

    return workspace


def _head(root: Path) -> str:
    argv = ["git", "rev-parse", "HEAD"]
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=True)  # noqa: S603
    return done.stdout.strip()


@asynccontextmanager
async def _runners(where: Path, setup: frameworks.TestSetup) -> AsyncGenerator[FixRunner]:
    yield LocalPytest(where, setup.project)


def _facts(**extra: Any) -> dict[str, Any]:
    return {"root_cause": ROOT_CAUSE, "repro": REPRO_FACT,
            "repro_result": ReproResult.REPRODUCED, "plan": PLAN, **extra}  # fmt: skip


# ---------------------------------------------------------------- root cause and plan


async def test_the_root_cause_gets_its_blast_radius(repo: Path) -> None:
    outcome = await root_cause_stage(_workspace(repo))(_stage(FixStage.ROOT_CAUSE, _facts()))
    assert isinstance(outcome, Advance)
    found = outcome.updates["root_cause"]
    assert found["blast_radius"] is None  # no graph here
    assert found["seen_in_repro"] is True  # the failure ran through app/stats.py
    gone = {**ROOT_CAUSE, "path": "app/gone.py"}
    stopped = await root_cause_stage(_workspace(repo))(
        _stage(FixStage.ROOT_CAUSE, _facts(root_cause=gone))
    )
    assert isinstance(stopped, Stop)
    assert "isn't in the repository" in stopped.reason


async def test_a_plan_is_checked_before_it_counts(repo: Path) -> None:
    touches_tests = {
        "summary": "Loosen the test",
        "risks": [],
        "steps": [{"description": "x", "path": "tests/test_ok.py"}],
        "files": [{"path": "tests/test_ok.py", "change": "modify", "why": "x"}],
    }
    good = {
        "summary": "Return 0 for an empty list.",
        "risks": ["Callers may expect an error"],
        "steps": [{"description": "Guard the empty list", "path": "app/stats.py"}],
        "files": [{"path": "app/stats.py", "change": "modify", "why": "the division"}],
    }
    fake = _fake([_tool("p1", "submit_plan", touches_tests)], [_tool("p2", "submit_plan", good)])
    steps: list[Step] = []
    outcome = await plan_stage(_workspace(repo), fake.client, None)(
        _stage(FixStage.PLAN, _facts(plan=None), steps)
    )  # fmt: skip
    assert isinstance(outcome, Advance), outcome
    plan = outcome.updates["plan"]
    assert [f["path"] for f in plan["files"]] == ["app/stats.py"]
    assert outcome.updates["risk"] is FixRisk.LOW
    assert plan["risk"]["level"] == "low"
    assert plan["prompt"] == "plan.v1"
    refused = next(s for s in steps if s.name == "submit_plan")
    assert "test or test setting" in refused.summary


# ---------------------------------------------------------------- patch


def _edit(call_id: str, new_body: str) -> dict[str, Any]:
    return _tool(call_id, "edit_file", {"path": "app/stats.py",
                                        "old_text": "    return sum(values) / len(values)\n",
                                        "new_text": new_body})  # fmt: skip


def _submit(call_id: str) -> dict[str, Any]:
    return _tool(call_id, "submit_patch", {"summary": "Guard the empty list.", "fixed": True})


async def _patch(repo: Path, fake: FakeAnthropic, steps: list[Step] | None = None) -> Any:
    handler = patch_stage(_workspace(repo), _runners, fake.client, None)
    return await handler(_stage(FixStage.PATCH, _facts(), steps))


async def test_a_patch_that_passes_is_kept_and_the_checkout_restored(repo: Path) -> None:
    fake = _fake([_edit("e1", FIXED_BODY)], [_tool("r1", "run_repro", {})], [_submit("s1")])
    steps: list[Step] = []
    outcome = await _patch(repo, fake, steps)
    assert isinstance(outcome, Advance), outcome
    patch = outcome.updates["patch"]
    assert patch["files"] == ["app/stats.py"]
    assert patch["lines"] == 2  # the guard added
    assert f"+++ b/{REPRO_PATH}" in patch["diff"]
    assert "+    if not values:" in patch["diff"]
    assert patch["baseline"]["ok"] is True
    assert patch["suite"]["ok"] is True
    assert [a["passed"] for a in patch["attempts"]] == [True]
    assert patch["risk"]["level"] == "low"
    assert (repo / "app/stats.py").read_text() == BAD  # restored
    assert not (repo / REPRO_PATH).exists()
    assert [s.name for s in steps if s.type == "verify"] == ["baseline suite", "attempt 1 of 4"]


async def test_a_failing_attempt_is_fed_back_and_retried(repo: Path) -> None:
    wrong = "    if not values:\n        return 1\n    return sum(values) / len(values)\n"
    fake = _fake(
        [_edit("e1", wrong)], [_submit("s1")],
        [_tool("e2", "edit_file", {"path": "app/stats.py", "old_text": "return 1",
                                   "new_text": "return 0"})],
        [_submit("s2")],
    )  # fmt: skip
    outcome = await _patch(repo, fake)
    assert isinstance(outcome, Advance), outcome
    attempts = outcome.updates["patch"]["attempts"]
    assert [a["passed"] for a in attempts] == [False, True]
    assert attempts[0]["reason"] == "The reproducing test still fails."
    assert "assert 1 == 0" in attempts[0]["output"]  # what was fed back
    fed_back = fake.bodies("/v1/messages")[2]["messages"][-1]["content"][0]["content"][0]["text"]
    assert fed_back.startswith("Attempt 1 of 4 failed")
    assert "<failure>" in fed_back


async def test_breaking_another_test_fails_the_attempt(repo: Path) -> None:
    breaks = "    if len(values) < 3:\n        return 0\n    return sum(values) / len(values)\n"
    fake = _fake([_edit("e1", breaks)], [_submit("s1")],
                 [_tool("g1", "submit_patch", {"summary": "Can't within the plan.",
                                               "fixed": False})])  # fmt: skip
    outcome = await _patch(repo, fake)
    assert isinstance(outcome, Stop)
    assert outcome.status is FixStatus.FAILED
    assert outcome.reason == "Couldn't produce a passing fix: Can't within the plan."
    first = outcome.updates["patch"]["attempts"][0]
    assert (
        "Tests that passed before fail with the fix: tests/test_ok.py::test_two"
        in (first["reason"])
    )


async def test_gaming_blocks_the_run_at_once(repo: Path) -> None:
    hides = ("    try:\n        return sum(values) / len(values)\n"
             "    except Exception:\n        return 0\n")  # fmt: skip
    fake = _fake([_edit("e1", hides)], [_submit("s1")])
    outcome = await _patch(repo, fake)
    assert isinstance(outcome, Stop)
    assert outcome.status is FixStatus.BLOCKED
    assert outcome.reason.startswith("Anti-gaming: broad_except (app/stats.py")
    assert [f["rule"] for f in outcome.updates["patch"]["findings"]] == ["broad_except"]


async def test_the_tools_refuse_tests_and_files_outside_the_plan(repo: Path) -> None:
    fake = _fake(
        [_tool("e1", "edit_file", {"path": "tests/test_ok.py", "old_text": "== 2",
                                   "new_text": "== 2 or True"}),
         _tool("e2", "edit_file", {"path": "app/__init__.py", "old_text": "x", "new_text": "y"}),
         _tool("e3", "create_file", {"path": REPRO_PATH, "content": "def test(): pass\n"})],
        [_tool("g1", "submit_patch", {"summary": "No.", "fixed": False})],
    )  # fmt: skip
    steps: list[Step] = []
    outcome = await _patch(repo, fake, steps)
    assert isinstance(outcome, Stop)
    said = [s.summary for s in steps if s.name in ("edit_file", "create_file")]
    assert said == ["not writable", "not writable", "not writable"]
    assert (repo / "tests/test_ok.py").read_text() == FILES["tests/test_ok.py"]


async def test_attempts_are_capped(repo: Path) -> None:
    wrong = "    if not values:\n        return 1\n    return sum(values) / len(values)\n"
    turns = [[_edit("e1", wrong)], *([[_submit(f"s{n}")] for n in range(MAX_ATTEMPTS)])]
    outcome = await _patch(repo, _fake(*turns))
    assert isinstance(outcome, Stop)
    assert outcome.status is FixStatus.FAILED
    assert len(outcome.updates["patch"]["attempts"]) == MAX_ATTEMPTS
    assert outcome.reason.startswith("Couldn't produce a passing fix: 4 attempts; the last: ")


# ---------------------------------------------------------------- verify


async def _patched(repo: Path) -> dict[str, Any]:
    fake = _fake([_edit("e1", FIXED_BODY)], [_submit("s1")])
    outcome = await _patch(repo, fake)
    assert isinstance(outcome, Advance)
    return dict(outcome.updates["patch"])


EXTRA = (
    "from app.stats import mean\n\n\ndef test_one_value():\n    assert mean([4]) == 4\n\n\n"
    "def test_negatives():\n    assert mean([-1, 1]) == 0\n"
)


def _verdict(call_id: str, *, shaped: bool = False) -> dict[str, Any]:
    return _tool(call_id, "submit_verdict", {
        "test_shaped": shaped, "reason": "It guards the empty case at its cause.",
        "concerns": [], "unverified": ["Callers that relied on the ZeroDivisionError"],
    })  # fmt: skip


async def test_a_verified_fix_gets_its_risk_and_readiness(repo: Path) -> None:
    patch = await _patched(repo)
    fake = _fake([_tool("w1", "write_extra_tests", {"content": EXTRA})],
                 [_tool("r1", "run_extra_tests", {})], [_verdict("v1")])  # fmt: skip
    steps: list[Step] = []
    handler = verify_stage(_workspace(repo), _runners, fake.client, None)
    outcome = await handler(_stage(FixStage.VERIFY, _facts(patch=patch), steps))
    assert isinstance(outcome, Advance), outcome
    found = outcome.updates["verification"]
    assert [r["passed"] for r in found["repro_runs"]] == [True, True, True]
    assert found["suite"]["ok"] is True
    assert found["extra_tests"]["passed"] is True
    assert found["verdict"]["test_shaped"] is False
    assert outcome.updates["risk"] is FixRisk.LOW
    assert outcome.updates["merge_readiness"] == 5
    assert found["unverified"] == [
        "Callers that relied on the ZeroDivisionError",
        "which existing tests reach the changed code (no graph yet)",
    ]
    assert fake.bodies("/v1/messages")[0]["model"].startswith("claude-opus")  # the verifier is Opus
    assert (repo / "app/stats.py").read_text() == BAD  # restored, extra tests gone
    assert not (repo / "tests/test_sensei_extra_0199f0000000.py").exists()


async def test_a_test_shaped_verdict_blocks_the_run(repo: Path) -> None:
    patch = await _patched(repo)
    fake = _fake([_verdict("v1", shaped=True)])
    handler = verify_stage(_workspace(repo), _runners, fake.client, None)
    outcome = await handler(_stage(FixStage.VERIFY, _facts(patch=patch)))
    assert isinstance(outcome, Stop)
    assert outcome.status is FixStatus.BLOCKED
    assert "test-shaped" in outcome.reason


async def test_a_patch_that_doesnt_apply_fails_verification(repo: Path) -> None:
    patch = await _patched(repo)
    (repo / "app/stats.py").write_text("def mean(values):\n    return 0\n")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "moved")
    handler = verify_stage(_workspace(repo), _runners, _fake().client, None)
    outcome = await handler(_stage(FixStage.VERIFY, _facts(patch=patch)))
    assert isinstance(outcome, Stop)
    assert outcome.status is FixStatus.FAILED
    assert outcome.reason.startswith("Verification: The patch doesn't apply")


async def test_a_plan_asked_to_change_is_the_next_revision(repo: Path) -> None:
    asked = {**PLAN, "revision": 1, "requests": [{
        "by": "bo", "comment": "Callers expect an error.", "revision": 1,
        "edits": {"steps": [{"description": "Raise ValueError", "path": "app/stats.py"}]},
    }]}  # fmt: skip
    good = {
        "summary": "Raise ValueError for an empty list.",
        "risks": [],
        "steps": [{"description": "Raise ValueError", "path": "app/stats.py"}],
        "files": [{"path": "app/stats.py", "change": "modify", "why": "the division"}],
    }
    fake = _fake([_tool("p1", "submit_plan", good)])
    outcome = await plan_stage(_workspace(repo), fake.client, None)(
        _stage(FixStage.PLAN, _facts(plan=asked))
    )
    assert isinstance(outcome, Advance), outcome
    assert outcome.updates["plan"]["revision"] == 2
    assert "requests" not in outcome.updates["plan"]  # the new plan answers them
    first = fake.bodies("/v1/messages")[0]["messages"][0]["content"]
    assert "Your earlier plan:" in first
    assert (
        "From @bo:\nCallers expect an error.\nTheir step: app/stats.py: Raise ValueError" in first
    )
