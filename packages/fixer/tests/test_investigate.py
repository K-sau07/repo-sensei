"""The investigation agent (8.5) on recorded answers, over a real git repository: the history
tools, findings checked before they count (paths, lines, the suspect commit), a submission
corrected after it was refused, a run that never submits, the model chosen by the report, and
the stage handler."""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from sensei_core.fixes.tickets import parse_ticket
from sensei_core.models import FixStage, FixStatus
from sensei_engine.agent.tools import ToolContext
from sensei_engine.llm.anthropic_client import Tier
from sensei_fixer.history import GitHistory, HistoryError
from sensei_fixer.investigate import (
    SUBMIT,
    FindingsError,
    Hooks,
    Investigator,
    check_findings,
    definitions,
    first_message,
    investigate_stage,
    plan_for,
)
from sensei_fixer.stages import Advance, StageContext, Step, Stop
from sensei_testing.anthropic import FakeAnthropic, message, sse

PATH = "/v1/messages"
STATS = "app/stats.py"
GOOD = (
    "def mean(values):\n    if not values:\n        return 0\n"
    "    return sum(values) / len(values)\n"
)
BAD = "def mean(values):\n    return sum(values) / len(values)\n"
TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/app/stats.py", line 2, in mean\n'
    "    return sum(values) / len(values)\n"
    "ZeroDivisionError: division by zero\n"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com", *args],  # noqa: S607
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository whose second commit drops the empty check: (root, that commit, head)."""
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / STATS).write_text(GOOD)
    (root / "README.md").write_text("# Stats\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Add mean")
    (root / STATS).write_text(BAD)
    _git(root, "commit", "-q", "-am", "Simplify mean")
    bad = _git(root, "rev-parse", "HEAD")
    return root, bad, bad


def _ctx(root: Path, sha: str) -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]  # the tools these runs call read files only
        org_id=None,  # type: ignore[arg-type]
        repo_id=None,  # type: ignore[arg-type]
        snapshot_id=None,  # type: ignore[arg-type]
        repo="o/stats", sha=sha, root=root, tree=["README.md", STATS],
    )  # fmt: skip


def _tool(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def _answer(content: list[dict[str, Any]], stop_reason: str = "tool_use") -> dict[str, Any]:
    found = message("", stop_reason=stop_reason)
    found["content"] = content
    return found


def _fake(*answers: dict[str, Any]) -> FakeAnthropic:
    queue = list(answers)

    def reply(_request: httpx2.Request) -> httpx2.Response:
        return sse(queue.pop(0))

    return FakeAnthropic(routes={PATH: reply})


def _findings(bad: str, path: str = STATS, line: int = 2) -> dict[str, Any]:
    return {
        "hypotheses": [{"claim": "mean divides by the length of an empty list", "likelihood": 0.9,
                        "evidence": [{"path": path, "start_line": line, "end_line": line}]}],
        "root_cause": {"claim": "the empty check was dropped", "path": path,
                       "start_line": 1, "end_line": line},
        "suspect_commit": bad,
        "confidence": "high",
    }  # fmt: skip


class Seen:
    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.spent: list[tuple[int, Decimal, float]] = []
        self.checks = 0

    def hooks(self) -> Hooks:
        async def record(step: Step) -> None:
            self.steps.append(step)

        async def spend(turns: int, cost: Decimal, seconds: float) -> None:
            self.spent.append((turns, cost, seconds))

        async def check() -> None:
            self.checks += 1

        return Hooks(record=record, spend=spend, check=check)


# ---------------------------------------------------------------- the history


async def test_the_history_says_who_changed_what(repo: tuple[Path, str, str]) -> None:
    root, bad, head = repo
    history = GitHistory(root, head)
    commits = await history.log(STATS)
    assert [c.subject for c in commits] == ["Simplify mean", "Add mean"]
    assert commits[0].author == "Ada"
    [line] = await history.blame(STATS, 2, 2)
    first = commits[1].sha
    # The line itself was written by the first commit: the bad one only took the check away
    # (which its diff shows).
    assert (line.sha, line.summary) == (first, "Add mean")
    assert line.text == "    return sum(values) / len(values)"
    shown = await history.show(bad, STATS)
    assert "-    if not values:" in shown
    assert await history.exists(bad[:12])
    assert not await history.exists("0" * 40)
    for refused in ("--output=/tmp/x", "../etc/passwd", "-p"):
        with pytest.raises(HistoryError):
            await history.blame(refused, 1, 1)
    with pytest.raises(HistoryError, match="isn't a commit id"):
        await history.show("HEAD; rm -rf /")


# ---------------------------------------------------------------- the findings


async def test_findings_are_checked_against_the_repository_and_its_history(
    repo: tuple[Path, str, str],
) -> None:
    root, bad, head = repo
    ctx, history = _ctx(root, head), GitHistory(root, head)
    found = await check_findings(ctx, history, _findings(bad))
    assert (found.where.path, found.suspect_commit, found.confidence) == (STATS, bad, "high")
    cases: list[tuple[dict[str, Any], str]] = [
        (_findings(bad, path="app/invented.py"), "isn't a file of the repository"),
        (_findings(bad, line=40), "aren't in it"),
        ({**_findings(bad), "suspect_commit": "0" * 40}, "isn't a commit before"),
        ({**_findings(bad), "hypotheses": []}, "one to five"),
        ({**_findings(bad), "confidence": "certain"}, "high, medium or low"),
    ]
    for wrong, said in cases:
        with pytest.raises(FindingsError, match=said):
            await check_findings(ctx, history, wrong)


# ---------------------------------------------------------------- the loop


async def test_an_investigation_reads_blames_and_submits(repo: tuple[Path, str, str]) -> None:
    root, bad, head = repo
    fake = _fake(
        _answer([_tool("t1", "read_code", {"path": STATS}),
                 _tool("t2", "git_blame", {"path": STATS, "start_line": 1, "end_line": 2})]),
        # A first submission citing a file that isn't there is refused; the second holds.
        _answer([_tool("t3", SUBMIT, _findings(bad, path="app/invented.py"))]),
        _answer([_tool("t4", SUBMIT, _findings(bad))]),
    )  # fmt: skip
    seen = Seen()
    done = await Investigator(fake.client()).run(
        _ctx(root, head), GitHistory(root, head), parse_ticket(TRACE, paths=[STATS]), seen.hooks()
    )
    assert done.findings is not None, done.reason
    assert done.findings.suspect_commit == bad
    assert done.turns == 3
    assert seen.checks == 3
    assert [s[0] for s in seen.spent] == [1, 1, 1]
    assert [s.name for s in seen.steps if s.type == "tool"] == [
        "read_code", "git_blame", SUBMIT, SUBMIT,
    ]  # fmt: skip
    bodies = fake.bodies(PATH)
    # The blame came back to the model: who wrote each line.
    blamed = next(r for r in bodies[1]["messages"][-1]["content"] if r["tool_use_id"] == "t2")
    assert "Ada" in json.dumps(blamed)
    assert "def mean(values):" in json.dumps(blamed)
    refused = bodies[2]["messages"][-1]["content"][0]
    assert refused["is_error"] is True
    assert "isn't a file of the repository" in json.dumps(refused)
    names = [tool["name"] for tool in bodies[0]["tools"]]
    assert names == [tool["name"] for tool in definitions()]
    assert "submit_evidence" not in names
    assert "<report>" in json.dumps(bodies[0]["messages"][0])  # the report, marked as data


async def test_a_run_that_never_submits_says_so(repo: tuple[Path, str, str]) -> None:
    root, _bad, head = repo
    talk = _answer([{"type": "text", "text": "It's probably the division."}], "end_turn")
    fake = _fake(talk, talk)
    done = await Investigator(fake.client()).run(
        _ctx(root, head), None, parse_ticket(TRACE, paths=[STATS]), Seen().hooks()
    )
    assert (done.findings, done.reason) == (None, "The investigation ended without findings.")
    reminder = fake.bodies(PATH)[1]["messages"][-1]["content"]
    assert "submit_findings" in reminder


def test_the_model_is_chosen_by_what_the_report_points_at() -> None:
    assert plan_for(parse_ticket(TRACE, paths=[STATS])) == (Tier.STANDARD, "medium")
    words = parse_ticket("The totals are off by a cent since Monday.")
    tier, effort = plan_for(words)
    assert (tier.value, effort) == ("deep", "high")  # nothing to start from but words
    assert "Innermost repository frame: app/stats.py:2" in first_message(
        parse_ticket(TRACE, paths=[STATS])
    )
    # What a retry added comes apart from the report, so a long trace can't crowd it out.
    long = parse_ticket(TRACE + "x" * 9_000, paths=[STATS])
    retried = long.model_copy(update={"context": ["From @ada: only with an empty cart"]})
    said = first_message(retried)
    assert "<context>\nFrom @ada: only with an empty cart\n</context>" in said
    assert said.index("</report>") < said.index("<context>")


# ---------------------------------------------------------------- the stage


async def test_the_stage_keeps_the_findings_or_stops(repo: tuple[Path, str, str]) -> None:
    root, bad, head = repo

    @asynccontextmanager
    async def workspace(_ctx_: StageContext) -> AsyncGenerator[tuple[ToolContext, GitHistory]]:
        yield _ctx(root, head), GitHistory(root, head)

    seen = Seen()
    stage = StageContext(
        fix_id=None, org_id=None, repo_id=None, stage=FixStage.INVESTIGATE,  # type: ignore[arg-type]
        ticket=parse_ticket(TRACE, paths=[STATS]), facts={},
        record=seen.hooks().record, spend=seen.hooks().spend, check=seen.hooks().check,
    )  # fmt: skip
    fake = _fake(_answer([_tool("t1", SUBMIT, _findings(bad))]))
    outcome = await investigate_stage(workspace, fake.client, None)(stage)
    assert isinstance(outcome, Advance)
    kept = outcome.updates["root_cause"]
    assert (kept["path"], kept["suspect_commit"]) == (STATS, bad)
    assert kept["hypotheses"][0]["likelihood"] == 0.9
    nothing = _fake(*[_answer([{"type": "text", "text": "Hm."}], "end_turn")] * 2)
    stopped = await investigate_stage(workspace, nothing.client, None)(stage)
    assert isinstance(stopped, Stop)
    assert stopped.status is FixStatus.FAILED
    assert stopped.reason == "Investigation: The investigation ended without findings."
