"""A requested change's plan (9.1) on recorded answers: checked before it counts (real files,
new tests only, patterns cited by lines the repository has), its files grouped by layer, its
tests, risk and revision; and a change's stages (no investigation or reproduction)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2

from sensei_core.fixes.tickets import Ticket
from sensei_core.models import FixKind, FixRisk, FixRun, FixStage, FixStatus
from sensei_engine.agent.tools import ToolContext
from sensei_engine.llm.anthropic_client import Tier
from sensei_fixer.change_plan import change_plan_stage, plan_for
from sensei_fixer.history import GitHistory
from sensei_fixer.stages import Advance, StageContext, Step, Stop, by_kind
from sensei_fixer.state import FixStateMachine
from sensei_testing.anthropic import FakeAnthropic, message, sse

FILES = {
    "pyproject.toml": "[project]\nname = 'items'\ndependencies = ['fastapi']\n",
    "app/__init__.py": "",
    "app/api/routes/items.py": "".join(f"# line {n}\n" for n in range(1, 41)),
    "app/api/routes/users.py": "def read_users():\n    return []\n",
    "tests/test_items.py": "def test_items():\n    pass\n",
}
PATTERN: dict[str, Any] = {
    "claim": "routes take dependencies like this", "path": "app/api/routes/items.py",
    "start_line": 3, "end_line": 9,
}  # fmt: skip
GOOD: dict[str, Any] = {
    "summary": "Limit each client to 60 item requests a minute.",
    "files": [
        {"path": "app/api/routes/items.py", "change": "modify", "why": "the routes to limit"},
        {"path": "app/api/limits.py", "change": "create", "why": "the limiter"},
    ],
    "tests": [{"path": "tests/test_rate_limit.py", "checks": "the 61st request is a 429"}],
    "patterns": [PATTERN],
    "steps": [
        {"description": "Add the limiter", "path": "app/api/limits.py"},
        {"description": "Depend on it in the item routes", "path": "app/api/routes/items.py"},
        {"description": "Test the limit", "path": "tests/test_rate_limit.py"},
    ],
    "risks": ["Clients behind one proxy share a limit"],
}  # fmt: skip


def _repo(tmp_path: Path) -> Path:
    for path, text in FILES.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    return tmp_path


def _tools(root: Path) -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]  # the tools these runs call read files only
        org_id=None,  # type: ignore[arg-type]
        repo_id=None,  # type: ignore[arg-type]
        snapshot_id=None,  # type: ignore[arg-type]
        repo="o/items", sha="c" * 40, root=root, tree=sorted(FILES),
    )  # fmt: skip


def _fake(*turns: list[dict[str, Any]]) -> FakeAnthropic:
    queue: list[dict[str, Any]] = []
    for content in turns:
        found = message("", stop_reason="tool_use")
        found["content"] = content
        queue.append(found)

    def reply(_request: httpx2.Request) -> httpx2.Response:
        return sse(queue.pop(0))

    return FakeAnthropic(routes={"/v1/messages": reply})


def _submit(call_id: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": call_id, "name": "submit_change_plan", "input": plan}]


def _stage(steps: list[Step], facts: dict[str, Any] | None = None) -> StageContext:
    async def record(step: Step) -> None:
        steps.append(step)

    async def spend(_t: int, _c: Decimal, _s: float) -> None:
        return None

    async def check() -> None:
        return None

    return StageContext(
        fix_id=uuid.uuid4(), org_id=None, repo_id=None, stage=FixStage.PLAN,  # type: ignore[arg-type]
        ticket=Ticket(title="Add rate limiting to the items API",
                      text="Add rate limiting to the items API (app/api/routes/items.py)."),
        facts=facts or {}, record=record, spend=spend, check=check, kind=FixKind.CHANGE,
    )  # fmt: skip


def _handler(root: Path, fake: FakeAnthropic) -> Any:
    @asynccontextmanager
    async def workspace(
        _ctx: StageContext,
    ) -> AsyncGenerator[tuple[ToolContext, GitHistory | None]]:
        yield _tools(root), None

    return change_plan_stage(workspace, fake.client, None)


async def test_a_change_plan_is_checked_then_grouped_by_layer(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    edits_a_test = {**GOOD, "tests": [{"path": "tests/test_items.py", "checks": "x"}]}
    bad_lines = {**GOOD, "patterns": [{**PATTERN, "end_line": 400}]}
    no_tests: dict[str, Any] = {**GOOD, "tests": []}
    fake = _fake(_submit("a", edits_a_test), _submit("b", bad_lines), _submit("c", no_tests),
                 _submit("d", GOOD))  # fmt: skip
    steps: list[Step] = []
    outcome = await _handler(root, fake)(_stage(steps))
    assert isinstance(outcome, Advance), outcome
    refused = [s.summary for s in steps if s.name == "submit_change_plan"]
    assert (
        refused[0]
        == "tests/test_items.py exists already: a change adds new tests, existing ones stay."
    )
    assert refused[1] == "app/api/routes/items.py has 40 lines: cite lines inside it, start <= end."
    assert refused[2] == "Add 1 to 5 new test files: a change brings its tests."
    plan = outcome.updates["plan"]
    assert [(f["path"], f["layer"]) for f in plan["files"]] == [
        ("app/api/routes/items.py", "api"), ("app/api/limits.py", "api"),
    ]  # fmt: skip
    assert plan["by_layer"] == {"api": ["app/api/routes/items.py", "app/api/limits.py"],
                                "test": ["tests/test_rate_limit.py"]}  # fmt: skip
    assert plan["patterns"][0]["start_line"] == 3
    assert (plan["revision"], plan["prompt"]) == (1, "change_plan.v1")
    assert outcome.updates["risk"] is FixRisk.LOW
    # The request names a file of the repository: Sonnet plans it.
    assert fake.bodies("/v1/messages")[0]["model"].startswith("claude-sonnet")


async def test_a_request_naming_nothing_is_planned_by_opus() -> None:
    assert plan_for("Add rate limiting", ["app/main.py"]) == (Tier.DEEP, "high")
    assert plan_for("Limit app/api/ routes", ["app/api/routes/items.py"]) == (
        Tier.STANDARD,
        "medium",
    )


async def test_a_fix_has_no_change_plan_and_a_change_no_reproduction() -> None:
    async def fails(_ctx: StageContext) -> Any:
        raise AssertionError("not for a change")

    handler = by_kind({FixKind.FIX: fails})
    stopped = await handler(_stage([]))
    assert isinstance(stopped, Stop)
    assert stopped.reason == "A change has no plan stage."
    run = FixRun(kind=FixKind.CHANGE, stage=FixStage.INTAKE, status=FixStatus.RUNNING)
    FixStateMachine(run).advance()
    assert run.stage is FixStage.PLAN  # no investigation, no reproduction
    run.status = FixStatus.RUNNING
    FixStateMachine(run).advance()
    assert (run.stage, run.status) == (FixStage.AWAITING_APPROVAL, FixStatus.AWAITING_APPROVAL)


async def test_a_new_test_must_be_a_test_file_in_one_project(tmp_path: Path) -> None:
    from sensei_fixer.change_plan import check_change_plan  # noqa: PLC0415
    from sensei_fixer.plan import PlanError  # noqa: PLC0415

    root = _repo(tmp_path)
    refused = ("tests/json.py", "tests/__init__.py", "tests/helpers.py", "__mocks__/axios.test.js",
               "tests/sitecustomize.py", "tests/conftest.py")  # fmt: skip
    for path in refused:
        plan = {**GOOD, "tests": [{"path": path, "checks": "x"}],
                "steps": [GOOD["steps"][0]]}  # fmt: skip
        try:
            await check_change_plan(_tools(root), plan)
        except PlanError:
            continue
        raise AssertionError(f"{path} was taken for a new test")
    (root / "web").mkdir()
    (root / "web/package.json").write_text('{"devDependencies": {"vitest": "5.0.0"}}')
    split = {**GOOD, "tests": [*GOOD["tests"], {"path": "web/tests/limit.test.js", "checks": "x"}]}
    try:
        await check_change_plan(_tools(root), split)
    except PlanError as error:
        assert "different projects" in str(error)  # noqa: PT017
    else:
        raise AssertionError("tests in two projects were taken")


async def test_a_changes_code_is_in_its_tests_project(tmp_path: Path) -> None:
    # Only the tests' project's suite is run: code of another project with tests of its own
    # would change with none of them run.
    from sensei_fixer.change_plan import check_change_plan  # noqa: PLC0415
    from sensei_fixer.plan import PlanError  # noqa: PLC0415

    root = _repo(tmp_path)
    (root / "web/src").mkdir(parents=True)
    (root / "web/package.json").write_text('{"devDependencies": {"vitest": "5.0.0"}}')
    across = {
        **GOOD,
        "files": [*GOOD["files"], {"path": "web/src/limit.ts", "change": "create", "why": "x"}],
    }
    try:
        await check_change_plan(_tools(root), across)
    except PlanError as error:
        assert "web/src/limit.ts is in web" in str(error)  # noqa: PT017
    else:
        raise AssertionError("code of another project was taken")
    assert (await check_change_plan(_tools(root), GOOD))["files"]
