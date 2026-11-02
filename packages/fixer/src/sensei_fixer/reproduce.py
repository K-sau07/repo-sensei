"""Reproduce first (8.6): a test that fails because of the bug, before anything is fixed.

A model writes one test file (at a path the pipeline chooses: new, the framework's naming, in
the project's test folder; nothing else can be written), runs it in the sandbox as often as it
needs, and submits it. The pipeline then runs it :data:`RUNS` times and judges each run: a run
reproduces the bug when it fails **for the reported reason** (the reported error's type in its
output, or a failed assertion when the report names no error), not when it can't even import or
collect (that reproduces nothing).

- every run reproduces: ``reproduced``, and the fix goes on;
- some do: ``flaky``, and the fix stops (a fix verified against a test that fails now and
  then proves nothing);
- none do (or no test was submitted): ``not_reproduced``, and the fix stops, its attempts
  listed. A person can retry it with more context.

The test and every run are kept on the fix (``repro``): the patch stage writes the test again
and verification runs it; the approval card shows its output.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, cast

from sensei_core.fixes.tickets import Ticket
from sensei_core.models import FixStatus, ReproResult
from sensei_core.usage import CostCalculator
from sensei_engine.agent.tools import ToolContext, run_tool
from sensei_engine.agent.tools.base import ToolResult
from sensei_engine.llm.anthropic_client import AnthropicClient, LLMTurn, Tier
from sensei_fixer.frameworks import NoFramework, TestSetup, detect
from sensei_fixer.history import GitHistory
from sensei_fixer.investigate import READ_TOOLS, Hooks, first_message
from sensei_fixer.runs import FixRunner, RunResult, SetupFailed
from sensei_fixer.stages import Advance, Outcome, StageContext, StageHandler, Step, Stop
from sensei_fixer.tooling import guarded, settle, turn_cost

PROMPT_VERSION = "reproduce.v1"
RUNS = 3
MAX_TURNS = 14
MAX_TRIES = 6  # run_test calls
MAX_TEST_BYTES = 20_000
SHOWN_OUTPUT = 2_500  # of a run's output, kept and shown
# The test didn't run at all (it reproduces nothing).
BROKEN = re.compile(
    r"ImportError|ModuleNotFoundError|SyntaxError|IndentationError|collected 0 items|"
    r"no tests ran|No test files found|Cannot find module|Failed to load|"
    r"error: unrecognized arguments|fixture '[^']+' not found|ReferenceError: \w+ is not defined"
)
FAILED = re.compile(
    r"\bFAILED\b|AssertionError|\bassert\b|expected|\u2717|\u00d7|\bfailed\b|Error:",
    re.IGNORECASE,
)
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "write_test",
        "description": "Write the reproducing test file (its whole content; the file and its "
        "place are fixed). Replaces what you wrote before.",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_test",
        "description": "Run the test file once in the sandbox: its exit and output, and whether "
        "it failed for the reported reason.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "submit_test",
        "description": "Done: the test as written reproduces the bug (one sentence on what it "
        "shows), or it can't be reproduced in a test (say why).",
        "input_schema": {
            "type": "object",
            "properties": {
                "explanation": {"type": "string"},
                "reproducible": {"type": "boolean"},
            },
            "required": ["explanation", "reproducible"],
            "additionalProperties": False,
        },
    },
)


def system_prompt() -> str:
    found = resources.files("sensei_fixer").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return found.read_text(encoding="utf-8")


def definitions() -> list[dict[str, Any]]:
    from sensei_engine.agent.tools import BY_NAME  # noqa: PLC0415 - the read tools' table

    return [*(BY_NAME[name].definition() for name in READ_TOOLS), *TOOLS]


# ---------------------------------------------------------------- judging runs


def for_the_reason(result: RunResult, ticket: Ticket) -> bool:
    """``result`` is a failure because of the reported bug (not a pass, a timeout, or a test
    that couldn't even run)."""
    if result.passed or result.timed_out or BROKEN.search(result.output):
        return False
    if ticket.error_type:
        name = ticket.error_type.rsplit(".", 1)[-1]
        message = (ticket.message or "").strip()[:60]
        return name in result.output or bool(message and message in result.output)
    return bool(FAILED.search(result.output))


def classify(results: Sequence[RunResult], ticket: Ticket) -> ReproResult:
    hits = [for_the_reason(r, ticket) for r in results]
    if hits and all(hits):
        return ReproResult.REPRODUCED
    if any(hits):
        return ReproResult.FLAKY
    return ReproResult.NOT_REPRODUCED


def _run_json(result: RunResult, ticket: Ticket) -> dict[str, Any]:
    return {
        "passed": result.passed, "exit_code": result.exit_code, "seconds": round(result.seconds, 2),
        "timed_out": result.timed_out, "for_reason": for_the_reason(result, ticket),
        "output": result.output[-SHOWN_OUTPUT:],
    }  # fmt: skip


# ---------------------------------------------------------------- the writer


@dataclass
class Attempt:
    """What the writer left: the test (None: it wrote none), why, and its tries."""

    content: str | None = None
    explanation: str = ""
    reproducible: bool = False
    submitted: bool = False
    tries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


def _text(text: str, summary: str, *, error: bool = False) -> ToolResult:
    return ToolResult([{"type": "text", "text": text}], [], summary, is_error=error)


def brief(ticket: Ticket, root_cause: Mapping[str, Any] | None, setup: TestSetup, path: str) -> str:
    lines = [first_message(ticket), ""]
    if root_cause:
        lines.append(f"The investigation's root cause: {root_cause.get('claim')} "
                     f"({root_cause.get('path')}:{root_cause.get('start_line')}-"
                     f"{root_cause.get('end_line')})")  # fmt: skip
        for hypothesis in cast("list[Mapping[str, Any]]", root_cause.get("hypotheses") or [])[:3]:
            lines.append(f"- {hypothesis.get('claim')} (likelihood {hypothesis.get('likelihood')})")
    lines += ["", f"Test framework: {setup.framework}, in the project {setup.project or '(root)'}.",
              f"Your test file: {path} (write_test sets its content)."]  # fmt: skip
    return "\n".join(lines)


@dataclass
class _Writing:
    """One attempt's state: where it reads, runs and writes, and what it has done."""

    ctx: ToolContext
    runner: FixRunner
    setup: TestSetup
    test_path: str
    ticket: Ticket
    hooks: Hooks
    attempt: Attempt = field(default_factory=Attempt)

    @property
    def target(self) -> Path:
        return self.ctx.root / self.test_path

    async def call(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        if name == "write_test":
            return await self.write_test(str(args.get("content", "")))
        if name == "run_test":
            return await self.run_test()
        if name == "submit_test":
            self.attempt.submitted = True
            self.attempt.explanation = str(args.get("explanation", ""))[:1000]
            self.attempt.reproducible = (
                bool(args.get("reproducible")) and self.attempt.content is not None
            )
            return _text("Submitted.", "submitted")
        if name in READ_TOOLS:
            return await guarded(name, run_tool(self.ctx, name, args))
        return _text(f"There is no tool {name!r}.", "unknown tool", error=True)

    async def write_test(self, content: str) -> ToolResult:
        if not content.strip() or len(content.encode()) > MAX_TEST_BYTES:
            return _text(f"A test file of 1 to {MAX_TEST_BYTES} bytes.", "bad test", error=True)
        await asyncio.to_thread(_write, self.target, content)
        self.attempt.content = content
        return _text(f"Written to {self.test_path}.", f"wrote {len(content.splitlines())} lines")

    async def run_test(self) -> ToolResult:
        if self.attempt.content is None:
            return _text("Write the test first (write_test).", "no test", error=True)
        if len(self.attempt.tries) >= MAX_TRIES:
            return _text(f"That was the last of {MAX_TRIES} runs: submit_test now.",
                         "no more runs", error=True)  # fmt: skip
        ran = await self.runner.run(self.setup.command(self.test_path))
        await self.hooks.spend(0, Decimal(0), ran.seconds)
        self.attempt.tries.append(_run_json(ran, self.ticket))
        if for_the_reason(ran, self.ticket):
            verdict = "It failed for the reported reason."
        elif ran.passed:
            verdict = "It passed: it doesn't reproduce the bug."
        else:
            verdict = "It failed, but not for the reported reason (or didn't run)."
        timed_out = " (timed out)" if ran.timed_out else ""
        body = f"Exit {ran.exit_code}{timed_out}. {verdict}\n\n{ran.output[-SHOWN_OUTPUT:]}"
        return _text(body, verdict)


def _write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@dataclass
class TestWriter:
    client: AnthropicClient
    costs: CostCalculator | None = None
    max_turns: int = MAX_TURNS

    async def write(self, writing: _Writing, root_cause: Mapping[str, Any] | None) -> Attempt:
        hooks, attempt = writing.hooks, writing.attempt
        first = brief(writing.ticket, root_cause, writing.setup, writing.test_path)
        messages: list[dict[str, Any]] = [{"role": "user", "content": first}]
        system, tools = (system_prompt(),), definitions()
        for turn in range(1, self.max_turns + 1):
            await hooks.check()
            started = time.monotonic()
            result = await self.client.turn(
                LLMTurn(tuple(messages), system, tools, Tier.STANDARD, effort="medium")
            )
            await hooks.spend(1, turn_cost(self.costs, result.usage), 0.0)
            calls = [b for b in result.content if b.get("type") == "tool_use"]
            named = ", ".join(c["name"] for c in calls) or "no tools"
            ms = round((time.monotonic() - started) * 1000)
            await hooks.record(Step("llm", f"turn {turn}", named, ms=ms))
            messages.append({"role": "assistant", "content": list(result.content)})
            if not calls or result.stop_reason == "refusal":
                attempt.explanation = attempt.explanation or "The writer stopped without a test."
                return attempt
            answers: list[dict[str, Any]] = []
            for call in calls:
                name = str(call["name"])
                done = await writing.call(name, cast("Mapping[str, Any]", call.get("input") or {}))
                await hooks.record(Step("tool", name, done.summary))
                answers.append(done.param(str(call["id"])))
                answers += done.notes(name)
            await settle(writing.ctx)
            if attempt.submitted:
                return attempt
            messages.append({"role": "user", "content": answers})
        attempt.explanation = f"No test was submitted in {self.max_turns} turns."
        return attempt


# ---------------------------------------------------------------- the stage


class Workspace(Protocol):
    def __call__(
        self, ctx: StageContext, /
    ) -> AbstractAsyncContextManager[tuple[ToolContext, GitHistory | None]]: ...


class Runners(Protocol):
    def __call__(
        self, root: Path, setup: TestSetup, /
    ) -> AbstractAsyncContextManager[FixRunner]: ...


def reproduce_stage(
    workspace: Workspace,
    runners: Runners,
    client: Callable[[], AnthropicClient],
    costs: CostCalculator | None,
) -> StageHandler:
    """The pipeline's ``reproduce`` handler."""

    async def handler(ctx: StageContext) -> Outcome:
        hooks = Hooks(record=ctx.record, spend=ctx.spend, check=ctx.check)
        root_cause = cast("Mapping[str, Any] | None", ctx.facts.get("root_cause"))
        culprit = (root_cause or {}).get("path") or (ctx.ticket.culprit.path
                                                       if ctx.ticket.culprit else None)  # fmt: skip
        async with workspace(ctx) as (tools, _history):
            try:
                setup = detect(tools.root, culprit)
            except NoFramework as error:
                return _stopped(str(error), None, [], None)
            path = setup.test_path(ctx.fix_id.hex[:12])
            try:
                async with runners(tools.root, setup) as runner:
                    writing = _Writing(tools, runner, setup, path, ctx.ticket, hooks)
                    attempt = await TestWriter(client(), costs).write(writing, root_cause)
                    runs = await _judge(runner, setup, path, attempt, hooks)
            except SetupFailed as error:
                return _stopped(str(error), None, [], setup)
            finally:
                (tools.root / path).unlink(missing_ok=True)  # the checkout as it was
        verdict = classify(runs, ctx.ticket) if runs else ReproResult.NOT_REPRODUCED
        repro = {
            "framework": setup.framework, "project": setup.project, "test_path": path,
            "test": attempt.content, "command": list(setup.command(path)),
            "explanation": attempt.explanation, "tries": attempt.tries,
            "runs": [_run_json(r, ctx.ticket) for r in runs],
        }  # fmt: skip
        updates = {"repro_result": verdict, "repro": repro}
        if verdict is ReproResult.REPRODUCED:
            return Advance(updates)
        failing = sum(1 for r in runs if for_the_reason(r, ctx.ticket))
        if verdict is ReproResult.FLAKY:
            reason = f"The test failed for the reported reason in {failing} of {RUNS} runs: flaky."
        elif not attempt.reproducible or attempt.content is None:
            reason = f"No reproducing test: {attempt.explanation or 'none was written'}"
        else:
            reason = f"The test didn't fail for the reported reason in {RUNS} runs."
        return Stop(FixStatus.NOT_REPRODUCED, reason, updates)

    return handler


async def _judge(
    runner: FixRunner, setup: TestSetup, path: str, attempt: Attempt, hooks: Hooks
) -> list[RunResult]:
    if attempt.content is None or not attempt.reproducible:
        return []
    runs: list[RunResult] = []
    for n in range(1, RUNS + 1):
        await hooks.check()
        ran = await runner.run(setup.command(path))
        await hooks.spend(0, Decimal(0), ran.seconds)
        said = "passed" if ran.passed else "failed"
        await hooks.record(Step("verify", f"run {n} of {RUNS}", said, ms=round(ran.seconds * 1000)))
        runs.append(ran)
    return runs


def _stopped(reason: str, content: str | None, runs: list[Any], setup: TestSetup | None) -> Stop:
    repro = {"framework": setup.framework if setup else None, "test": content, "runs": runs,
             "explanation": reason}  # fmt: skip
    return Stop(FixStatus.NOT_REPRODUCED, reason,
                {"repro_result": ReproResult.NOT_REPRODUCED, "repro": repro})  # fmt: skip
