"""A requested change built and verified (9.2) on recorded answers, with real git and pytest:
its code and its new tests written within the approved plan, its tests passing with it and
failing without it (tests that pass either way are refused), existing tests untouched, the
whole suite held to its baseline; then verified from a clean checkout."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sensei_core.fixes.tickets import Ticket
from sensei_core.models import FixKind, FixRisk, FixStage, FixStatus
from sensei_engine.agent.tools import ToolContext
from sensei_fixer import frameworks
from sensei_fixer.history import GitHistory
from sensei_fixer.patch import patch_stage
from sensei_fixer.runs import FixRunner
from sensei_fixer.stages import Advance, StageContext, Step, Stop
from sensei_fixer.verify import verify_stage
from sensei_testing.anthropic import FakeAnthropic
from sensei_testing.fixer import LocalPytest, git, git_repo, scripted, tool_use

STATS = "def mean(values):\n    return sum(values) / len(values)\n"
FILES = {
    "pyproject.toml": "[project]\nname = 'stats'\n",
    "conftest.py": "",
    "app/__init__.py": "",
    "app/stats.py": STATS,
    "tests/test_mean.py": "from app.stats import mean\n\n\ndef test_mean():\n"
    "    assert mean([1, 3]) == 2\n",
}
MEDIAN = (
    "\n\ndef median(values):\n    ordered = sorted(values)\n    middle = len(ordered) // 2\n"
    "    if len(ordered) % 2:\n        return ordered[middle]\n"
    "    return (ordered[middle - 1] + ordered[middle]) / 2\n"
)
MEDIAN_TESTS = (
    "from app import stats\n\n\ndef test_odd():\n    assert stats.median([3, 1, 2]) == 2\n\n\n"
    "def test_even():\n    assert stats.median([4, 1, 3, 2]) == 2.5\n"
)
PLAN: dict[str, Any] = {
    "summary": "Add median() beside mean().", "revision": 1, "risk": {"level": "low"},
    "files": [{"path": "app/stats.py", "change": "modify", "why": "where mean is",
               "layer": "svc"}],
    "tests": [{"path": "tests/test_median.py", "checks": "odd and even lengths",
               "layer": "test"}],
    "steps": [{"description": "Add median", "path": "app/stats.py"},
              {"description": "Test it", "path": "tests/test_median.py"}],
    "blast_radius": None,
}  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return git_repo(tmp_path / "repo", FILES)


def _tools(root: Path) -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]  # the tools these runs call read files only
        org_id=None,  # type: ignore[arg-type]
        repo_id=None,  # type: ignore[arg-type]
        snapshot_id=None,  # type: ignore[arg-type]
        repo="o/stats", sha="d" * 40, root=root, tree=sorted(FILES),
    )  # fmt: skip


def _stage(stage: FixStage, facts: dict[str, Any], steps: list[Step] | None = None) -> StageContext:
    async def record(step: Step) -> None:
        if steps is not None:
            steps.append(step)

    async def spend(_t: int, _c: Decimal, _s: float) -> None:
        return None

    async def check() -> None:
        return None

    return StageContext(
        fix_id=uuid.UUID("0199f000-0000-7000-8000-000000000cde"), org_id=None,  # type: ignore[arg-type]
        repo_id=None, stage=stage,  # type: ignore[arg-type]
        ticket=Ticket(title="Add median()", text="Add a median() beside mean() in app/stats.py."),
        facts=facts, record=record, spend=spend, check=check, kind=FixKind.CHANGE,
    )  # fmt: skip


def _workspace(root: Path) -> Any:
    @asynccontextmanager
    async def workspace(_ctx: StageContext) -> AsyncGenerator[tuple[ToolContext, GitHistory]]:
        yield _tools(root), GitHistory(root, git(root, "rev-parse", "HEAD").strip())

    return workspace


@asynccontextmanager
async def _runners(where: Path, setup: frameworks.TestSetup) -> AsyncGenerator[FixRunner]:
    yield LocalPytest(where, setup.project)


def _create(call_id: str, path: str, content: str) -> dict[str, Any]:
    return tool_use(call_id, "create_file", {"path": path, "content": content})


def _add_median(call_id: str) -> dict[str, Any]:
    return tool_use(call_id, "edit_file", {"path": "app/stats.py", "old_text": STATS,
                                           "new_text": STATS + MEDIAN})  # fmt: skip


def _submit(call_id: str) -> dict[str, Any]:
    return tool_use(call_id, "submit_patch", {"summary": "median() added.", "fixed": True})


async def _patch(repo: Path, fake: FakeAnthropic, steps: list[Step] | None = None) -> Any:
    handler = patch_stage(_workspace(repo), _runners, fake.client, None)
    return await handler(_stage(FixStage.PATCH, {"plan": PLAN}, steps))


async def test_a_change_is_built_with_tests_that_need_it(repo: Path) -> None:
    fake = scripted(
        [_create("t1", "tests/test_median.py", MEDIAN_TESTS), _add_median("e1")],
        [tool_use("r1", "run_new_tests", {})], [_submit("s1")],
    )  # fmt: skip
    outcome = await _patch(repo, fake)
    assert isinstance(outcome, Advance), outcome
    patch = outcome.updates["patch"]
    assert (patch["files"], patch["tests"]) == (["app/stats.py"], ["tests/test_median.py"])
    assert "+++ b/tests/test_median.py" in patch["diff"]
    assert "+def median(values):" in patch["diff"]
    assert patch["prompt"] == "change_patch.v1"
    assert [a["passed"] for a in patch["attempts"]] == [True]
    assert (repo / "app/stats.py").read_text() == STATS  # the checkout as it was
    assert not (repo / "tests/test_median.py").exists()
    tools = fake.bodies("/v1/messages")[0]["tools"]
    assert "run_new_tests" in {t["name"] for t in tools}
    assert "run_repro" not in {t["name"] for t in tools}


async def test_tests_that_pass_without_the_change_are_refused(repo: Path) -> None:
    # Tests of mean() pass with or without median(): they don't test the change.
    idle = "from app.stats import mean\n\n\ndef test_mean_again():\n    assert mean([2]) == 2\n"
    fake = scripted(
        [_create("t1", "tests/test_median.py", idle), _add_median("e1")], [_submit("s1")],
        [tool_use("g1", "submit_patch", {"summary": "No.", "fixed": False})],
    )  # fmt: skip
    outcome = await _patch(repo, fake)
    assert isinstance(outcome, Stop)
    assert outcome.status is FixStatus.FAILED
    [first] = outcome.updates["patch"]["attempts"]
    assert first["reason"].startswith("The new tests pass without the change too")


async def test_existing_tests_stay_and_planned_tests_must_be_written(repo: Path) -> None:
    fake = scripted(
        [tool_use("e1", "edit_file", {"path": "tests/test_mean.py", "old_text": "== 2",
                                      "new_text": "== 2 or True"}), _add_median("e2")],
        [_submit("s1")],
        [tool_use("g1", "submit_patch", {"summary": "No.", "fixed": False})],
    )  # fmt: skip
    steps: list[Step] = []
    outcome = await _patch(repo, fake, steps)
    assert isinstance(outcome, Stop)
    assert next(s.summary for s in steps if s.name == "edit_file") == "not writable"
    [first] = outcome.updates["patch"]["attempts"]
    # Without its test the change is blocked: a planned test missing is an anti-gaming finding.
    assert first["blocked"] is True
    assert outcome.status is FixStatus.BLOCKED
    assert "test_missing" in {f["rule"] for f in outcome.updates["patch"]["findings"]}


EXTRA = "from app.stats import median\n\n\ndef test_one():\n    assert median([7]) == 7\n"


async def test_a_built_change_is_verified_from_a_clean_checkout(repo: Path) -> None:
    built = await _patch(repo, scripted(
        [_create("t1", "tests/test_median.py", MEDIAN_TESTS), _add_median("e1")],
        [_submit("s1")],
    ))  # fmt: skip
    assert isinstance(built, Advance)
    verdict: dict[str, Any] = {
        "test_shaped": False, "reason": "median() does what was asked.", "concerns": [],
        "unverified": [],
    }  # fmt: skip
    fake = scripted([tool_use("w1", "write_extra_tests", {"content": EXTRA})],
                    [tool_use("v1", "submit_verdict", verdict)])  # fmt: skip
    handler = verify_stage(_workspace(repo), _runners, fake.client, None)
    facts = {"plan": PLAN, "patch": built.updates["patch"]}
    outcome = await handler(_stage(FixStage.VERIFY, facts))
    assert isinstance(outcome, Advance), outcome
    found = outcome.updates["verification"]
    assert [r["passed"] for r in found["repro_runs"]] == [True, True, True]
    assert [r["passed"] for r in found["without_change"]] == [False]  # its tests need it
    assert found["suite"]["ok"] is True
    assert found["prompt"] == "change_verify.v1"
    assert outcome.updates["risk"] is FixRisk.LOW
    assert "The requested change" in fake.bodies("/v1/messages")[0]["messages"][0]["content"]
    assert (repo / "app/stats.py").read_text() == STATS
