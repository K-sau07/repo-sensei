"""Reproduce first (8.6): which framework a test is written for, how runs are judged (for the
reported reason, not merely failing), and the stage on recorded answers with real pytest runs:
reproduced, not reproduced, flaky, a test that can't import, no framework."""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from sensei_core.fixes.tickets import parse_ticket
from sensei_core.models import FixStage, FixStatus, ReproResult
from sensei_engine.agent.tools import ToolContext
from sensei_fixer import frameworks  # its TestSetup, not a test class to collect
from sensei_fixer.frameworks import NoFramework, detect
from sensei_fixer.history import GitHistory
from sensei_fixer.reproduce import classify, for_the_reason, reproduce_stage
from sensei_fixer.runs import FixRunner, RunResult
from sensei_fixer.stages import Advance, StageContext, Step, Stop
from sensei_testing.anthropic import FakeAnthropic, message, sse

PATH = "/v1/messages"
TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/app/stats.py", line 2, in mean\n'
    "    return sum(values) / len(values)\n"
    "ZeroDivisionError: division by zero\n"
)
# A failure through the reported code, as pytest shows it.
THROUGH = "app/stats.py:2: ZeroDivisionError\nE   ZeroDivisionError: division by zero"
BAD = "def mean(values):\n    return sum(values) / len(values)\n"
REPRODUCING = "from app.stats import mean\n\n\ndef test_empty():\n    assert mean([]) == 0\n"
PASSING = "from app.stats import mean\n\n\ndef test_two():\n    assert mean([1, 3]) == 2\n"
BROKEN = "from app.missing import mean\n\n\ndef test_empty():\n    assert mean([]) == 0\n"


# ---------------------------------------------------------------- frameworks


def _files(root: Path, files: dict[str, str]) -> Path:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


def test_the_framework_is_the_nearest_projects(tmp_path: Path) -> None:
    root = _files(tmp_path, {
        "backend/pyproject.toml": "[project]\nname='b'\n", "backend/tests/test_x.py": "",
        "backend/app/stats.py": BAD,
        "web/package.json": '{"devDependencies": {"vitest": "5.0.0"}}', "web/tsconfig.json": "{}",
        "web/src/lib/money.ts": "", "admin/package.json": '{"devDependencies": {"jest": "30"}}',
        "admin/src/a.js": "",
    })  # fmt: skip
    py = detect(root, "backend/app/stats.py")
    assert (py.framework, py.project, py.test_dir) == ("pytest", "backend", "backend/tests")
    path = py.test_path("abc123def456")
    assert path == "backend/tests/test_sensei_repro_abc123def456.py"
    assert py.command(path)[-1] == "tests/test_sensei_repro_abc123def456.py"
    web = detect(root, "web/src/lib/money.ts")
    assert (web.framework, web.project, web.test_dir) == ("vitest", "web", "web/tests")
    assert web.test_path("abc123def456").endswith(".test.ts")
    admin = detect(root, "admin/src/a.js")
    assert (admin.framework, admin.typescript) == ("jest", False)
    assert admin.test_path("abc123def456").endswith(".test.js")
    with pytest.raises(NoFramework):
        detect(_files(tmp_path / "none", {"README.md": "x"}), "lib/x.go")
    with pytest.raises(ValueError, match="not a fix id"):
        py.test_path("../../etc")


# ---------------------------------------------------------------- judging runs


def _ran(passed: bool, output: str, *, timed_out: bool = False) -> RunResult:
    return RunResult(passed, 0 if passed else 1, output, 0.5, timed_out)


def test_a_run_reproduces_only_when_it_fails_for_the_reported_reason() -> None:
    crash = parse_ticket(TRACE)
    assert for_the_reason(_ran(False, "E   ZeroDivisionError: division by zero\n1 failed"), crash)
    assert not for_the_reason(_ran(True, "1 passed"), crash)
    assert not for_the_reason(_ran(False, "E   KeyError: 'x'\n1 failed"), crash)  # another error
    assert not for_the_reason(_ran(False, "ModuleNotFoundError: No module named 'app'"), crash)
    assert not for_the_reason(_ran(False, "", timed_out=True), crash)
    wrong_value = parse_ticket("Totals are a cent short on invoices with a discount.")
    assert for_the_reason(_ran(False, "E   assert 1999 == 2000\n1 failed"), wrong_value)
    runs = [
        _ran(False, "ZeroDivisionError"),
        _ran(True, "1 passed"),
        _ran(False, "ZeroDivisionError"),
    ]
    assert classify(runs, crash) is ReproResult.FLAKY
    assert classify(runs[:1] * 3, crash) is ReproResult.REPRODUCED
    assert classify([runs[1]] * 3, crash) is ReproResult.NOT_REPRODUCED


# ---------------------------------------------------------------- the stage


@dataclass
class LocalPytest:
    """Our own tests' pytest, on this machine (the test files are the scripted model's: ours)."""

    root: Path
    project: str
    calls: list[Sequence[str]] = field(default_factory=list[Sequence[str]])

    async def run(self, argv: Sequence[str]) -> RunResult:
        self.calls.append(argv)
        cmd = [sys.executable if argv[0] == "python" else argv[0], *argv[1:]]
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.root / self.project, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        out, _ = await process.communicate()
        code = process.returncode
        return RunResult(code == 0, code, out.decode(), time.monotonic() - started)


@dataclass
class Alternating:
    """Fails, passes, fails...: a flaky test (the writer's own run is the first)."""

    n: int = 0

    async def run(self, argv: Sequence[str]) -> RunResult:
        self.n += 1
        if self.n % 2:
            return _ran(False, THROUGH + "\n1 failed")
        return _ran(True, "1 passed")


def _project(tmp_path: Path) -> Path:
    return _files(tmp_path / "repo", {
        "pyproject.toml": "[project]\nname='stats'\n", "conftest.py": "",
        "app/__init__.py": "", "app/stats.py": BAD, "tests/test_ok.py": "def test_ok(): pass\n",
    })  # fmt: skip


def _ctx(root: Path) -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]  # the tools these runs call read files only
        org_id=None,  # type: ignore[arg-type]
        repo_id=None,  # type: ignore[arg-type]
        snapshot_id=None,  # type: ignore[arg-type]
        repo="o/stats", sha="b" * 40, root=root, tree=["app/stats.py", "tests/test_ok.py"],
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

    return FakeAnthropic(routes={PATH: reply})


def _writes(test: str) -> FakeAnthropic:
    return _fake(
        [_tool("t1", "read_code", {"path": "tests/test_ok.py"}), _tool("t2", "write_test",
                                                                       {"content": test})],
        [_tool("t3", "run_test", {})],
        [_tool("t4", "submit_test", {"explanation": "mean([]) divides by zero",
                                     "reproducible": True})],
    )  # fmt: skip


def _stage(steps: list[Step]) -> StageContext:
    async def record(step: Step) -> None:
        steps.append(step)

    async def spend(_t: int, _c: Decimal, _s: float) -> None:
        return None

    async def check() -> None:
        return None

    return StageContext(
        fix_id=uuid.UUID("0199f000-0000-7000-8000-000000000abc"), org_id=None,  # type: ignore[arg-type]
        repo_id=None, stage=FixStage.REPRODUCE,  # type: ignore[arg-type]
        ticket=parse_ticket(TRACE, paths=["app/stats.py"]),
        facts={"root_cause": {"claim": "no empty check", "path": "app/stats.py",
                              "start_line": 1, "end_line": 2, "hypotheses": []}},
        record=record, spend=spend, check=check,
    )  # fmt: skip


def _handler(root: Path, fake: FakeAnthropic, runner: FixRunner | None = None) -> Any:
    @asynccontextmanager
    async def workspace(
        _ctx_: StageContext,
    ) -> AsyncGenerator[tuple[ToolContext, GitHistory | None]]:
        yield _ctx(root), None

    @asynccontextmanager
    async def runners(where: Path, setup: frameworks.TestSetup) -> AsyncGenerator[FixRunner]:
        yield runner or LocalPytest(where, setup.project)

    return reproduce_stage(workspace, runners, fake.client, None)


async def test_a_test_that_fails_for_the_reason_reproduces_it(tmp_path: Path) -> None:
    root = _project(tmp_path)
    steps: list[Step] = []
    outcome = await _handler(root, _writes(REPRODUCING))(_stage(steps))
    assert isinstance(outcome, Advance), outcome
    repro = outcome.updates["repro"]
    assert outcome.updates["repro_result"] is ReproResult.REPRODUCED
    assert repro["test"] == REPRODUCING
    assert repro["test_path"].startswith("tests/test_sensei_repro_")
    assert [r["for_reason"] for r in repro["runs"]] == [True, True, True]
    assert "ZeroDivisionError" in repro["runs"][0]["output"]
    assert len(repro["tries"]) == 1  # the writer's own run, before the three
    assert not (root / repro["test_path"]).exists()  # the checkout as it was
    assert [s.name for s in steps if s.type == "verify"] == ["run 1 of 3", "run 2 of 3",
                                                           "run 3 of 3"]  # fmt: skip


async def test_a_passing_or_broken_test_reproduces_nothing(tmp_path: Path) -> None:
    for test in (PASSING, BROKEN):
        root = _project(tmp_path / ("p" if test is PASSING else "b"))
        outcome = await _handler(root, _writes(test))(_stage([]))
        assert isinstance(outcome, Stop)
        assert outcome.status is FixStatus.NOT_REPRODUCED
        assert outcome.updates["repro_result"] is ReproResult.NOT_REPRODUCED
        assert outcome.reason == "The test didn't fail for the reported reason in 3 runs."


async def test_a_flaky_test_stops_the_fix(tmp_path: Path) -> None:
    root = _project(tmp_path)
    outcome = await _handler(root, _writes(REPRODUCING), Alternating())(_stage([]))
    assert isinstance(outcome, Stop)
    assert (outcome.status, outcome.updates["repro_result"]) == (
        FixStatus.NOT_REPRODUCED, ReproResult.FLAKY,
    )  # fmt: skip
    assert outcome.reason == "The test failed for the reported reason in 1 of 3 runs: flaky."


async def test_an_unreproducible_bug_or_no_framework_says_so(tmp_path: Path) -> None:
    root = _project(tmp_path)
    honest = _fake([_tool("t1", "submit_test", {"explanation": "It needs a real browser.",
                                                "reproducible": False})])  # fmt: skip
    outcome = await _handler(root, honest)(_stage([]))
    assert isinstance(outcome, Stop)
    assert outcome.reason == "No reproducing test: It needs a real browser."
    bare = _files(tmp_path / "bare", {"app/stats.py": BAD})
    nothing = await _handler(bare, _fake())(_stage([]))
    assert isinstance(nothing, Stop)
    assert "No pytest, vitest or jest project" in nothing.reason


def test_the_reason_is_an_exception_through_the_reported_code() -> None:
    crash = parse_ticket(TRACE, paths=["app/stats.py"])
    paths = ["app/stats.py"]
    through = "app/stats.py:2: ZeroDivisionError\nE   ZeroDivisionError: division by zero"
    assert for_the_reason(_ran(False, through + "\n1 failed"), crash, paths)
    # The test raising the error itself: its traceback never reaches the reported code.
    by_hand = "tests/test_x.py:4: ZeroDivisionError\nE   ZeroDivisionError: division by zero"
    assert not for_the_reason(_ran(False, by_hand + "\n1 failed"), crash, paths)
    # The error's name merely written somewhere (the test's own source, a message).
    named = "    assert False, 'no ZeroDivisionError here'\nE   AssertionError\n1 failed"
    assert not for_the_reason(_ran(False, named), crash)
    # A reported ImportError can be reproduced (it isn't a test that couldn't run).
    imports = parse_ticket("Traceback (most recent call last):\n"
                           '  File "/srv/app/app/plugins.py", line 3, in load\n'
                           "ModuleNotFoundError: No module named 'app.extras'\n",
                           paths=["app/plugins.py"])  # fmt: skip
    failing = ("app/plugins.py:3: in load\n"
               "E   ModuleNotFoundError: No module named 'app.extras'\n1 failed")  # fmt: skip
    assert for_the_reason(_ran(False, failing), imports, ["app/plugins.py"])


def test_a_project_behind_a_symbolic_link_isnt_written_to(tmp_path: Path) -> None:
    outside = _files(tmp_path / "outside", {"pyproject.toml": "[project]\nname='x'\n"})
    root = _files(tmp_path / "repo", {"README.md": "x"})
    (root / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(NoFramework):
        detect(root, "evil/app.py")  # its pyproject is outside the checkout: not a project


def test_only_a_traceback_frame_says_where_a_failure_ran() -> None:
    crash = parse_ticket(TRACE, paths=["app/stats.py"])
    # The test raising the error itself, a comment naming the reported file: no frame there.
    commented = (
        "    def test_x():\n        # app/stats.py\n>       raise ZeroDivisionError\n"
        "E   ZeroDivisionError\n\ntests/test_x.py:4: ZeroDivisionError\n1 failed"
    )
    assert not for_the_reason(_ran(False, commented), crash, ["app/stats.py"])
    js = "TypeError: Cannot read it\n    at total (/work/web/src/lib/money.ts:12:5)"
    from sensei_fixer.reproduce import frames_in  # noqa: PLC0415

    assert frames_in(js) == ["/work/web/src/lib/money.ts"]
