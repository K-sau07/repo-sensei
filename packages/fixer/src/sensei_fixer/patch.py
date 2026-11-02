"""The patch (8.7): after a person approved the plan, a minimal change that makes the
reproducing test pass and keeps the rest of the suite as it was.

First the whole suite runs at the commit being fixed (its **baseline**: what already fails in
the sandbox isn't the fix's to answer for), then the reproducing test is written back as it
reproduced the bug. A model (Sonnet) changes the code with tools that can only touch the
approved plan's files (``edit_file`` replaces one exact passage; ``create_file`` makes a file
the plan creates), never a test, can run the reproducing test and the repository's own test
files, and submits. Each submission is checked, in order:

1. the anti-gaming checks (:mod:`.antigaming`): a hit **blocks the run**, no retry;
2. the change is no riskier than the plan people approved (:mod:`.risk`);
3. the reproducing test passes;
4. the whole suite passes against its baseline (:mod:`.suites`).

A submission that fails 2-4 is answered with why and the failing output, and the model tries
again: :data:`MAX_ATTEMPTS` submissions in all (three retries). The change that passed is kept
on the fix (``patch``): its diff, files, size, the attempts, and the baseline for verification.
The checkout is restored afterwards.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, cast

from sensei_core.fixes.tickets import Ticket
from sensei_core.models import FixRisk, FixStatus
from sensei_core.usage import CostCalculator
from sensei_engine.agent.tools import BY_NAME, ToolContext, run_tool
from sensei_engine.agent.tools.base import ToolResult
from sensei_engine.llm.anthropic_client import AnthropicClient, Tier
from sensei_fixer import antigaming
from sensei_fixer.agents import Conversation
from sensei_fixer.frameworks import NoFramework, TestSetup, detect
from sensei_fixer.investigate import READ_TOOLS, Hooks, fenced, first_message
from sensei_fixer.patching import Diff, PatchError, diff_of, read_text, restore, write_file
from sensei_fixer.reproduce import Runners, Workspace
from sensei_fixer.risk import Areas, Risk, assess, code_areas, graph_areas, higher, path_areas
from sensei_fixer.runs import FixRunner, RunResult, SetupFailed
from sensei_fixer.stages import Advance, Outcome, StageContext, StageHandler, Step, Stop
from sensei_fixer.suites import SuiteRun, SuiteVerdict, compare, read_suite
from sensei_fixer.tooling import text_result

PROMPT_VERSION = "patch.v1"
MAX_ATTEMPTS = 4  # submissions: the first and three retries
MAX_TURNS = 30
MAX_RUNS = 12  # run_repro and run_tests calls
SHOWN = 3_000  # of a failing run's output, fed back
SUBMIT = "submit_patch"
REMINDER = (
    "Change the code with edit_file (or create_file), check it with run_repro, then call "
    "submit_patch. If you can't fix it within the plan, call submit_patch saying so."
)
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "edit_file",
        "description": "Replace one exact passage of a file the plan changes: old_text must "
        "appear exactly once in the file (copy it from read_code, with its indentation); it "
        "becomes new_text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_file",
        "description": "Create a file the plan creates (its whole content).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_repro",
        "description": "Run the reproducing test once in the sandbox, with your change: its "
        "exit and output.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "run_tests",
        "description": "Run one of the repository's own test files in the sandbox, with your "
        "change: its exit and output.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": SUBMIT,
        "description": "Submit your change for checking (the reproducing test, then the whole "
        "suite): one or two sentences on what it changes and why. If it fails, you are told why "
        "and can try again. Set fixed false if you can't fix it within the plan (say why).",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "fixed": {"type": "boolean"}},
            "required": ["summary", "fixed"],
            "additionalProperties": False,
        },
    },
)


def system_prompt() -> str:
    found = resources.files("sensei_fixer").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return found.read_text(encoding="utf-8")


def definitions() -> list[dict[str, Any]]:
    return [*(BY_NAME[name].definition() for name in READ_TOOLS), *TOOLS]


def _shown(result: RunResult) -> str:
    timed_out = " (timed out)" if result.timed_out else ""
    return f"Exit {result.exit_code}{timed_out}.\n\n{result.output[-SHOWN:]}"


@dataclass(frozen=True, slots=True)
class Approved:
    """What the run holds a patch to: the plan's files, its risk, the reproducing test."""

    planned: frozenset[str]
    creates: frozenset[str]
    risk: FixRisk
    repro_path: str
    repro_test: str
    plan_text: str


def approved(facts: Mapping[str, Any]) -> Approved | str:
    """The approved plan and reproducing test in ``facts`` (or why they're missing)."""
    plan = cast("Mapping[str, Any] | None", facts.get("plan"))
    repro = cast("Mapping[str, Any] | None", facts.get("repro"))
    if not plan or not plan.get("files"):
        return "There is no approved plan."
    if not repro or not repro.get("test") or not repro.get("test_path"):
        return "There is no reproducing test."
    files = cast("list[Mapping[str, Any]]", plan["files"])
    level = cast("Mapping[str, Any]", plan.get("risk") or {}).get("level") or FixRisk.HIGH.value
    steps = cast("list[Mapping[str, Any]]", plan.get("steps") or [])
    text = "\n".join([f"Summary: {plan.get('summary')}",
                      *(f"- {s.get('path')}: {s.get('description')}" for s in steps)])  # fmt: skip
    return Approved(
        planned=frozenset(str(f["path"]) for f in files),
        creates=frozenset(str(f["path"]) for f in files if f.get("change") == "create"),
        risk=FixRisk(level), repro_path=str(repro["test_path"]), repro_test=str(repro["test"]),
        plan_text=text,
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class Checked:
    """A submission's checks: blocked (the rules hit), passed, or failed (why, to feed back)."""

    passed: bool
    reason: str
    findings: tuple[antigaming.Finding, ...] = ()
    risk: Risk | None = None
    suite: SuiteRun | None = None
    known_failures: tuple[str, ...] = ()
    output: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.findings)


@dataclass(frozen=True, slots=True)
class Job:
    """What the patch and verify stages work with: the checkout and its tools, a runner in the
    sandbox, the test setup, the approved plan, and the fix's hooks and model."""

    ctx: StageContext
    tools: ToolContext
    runner: FixRunner
    setup: TestSetup
    plan: Approved
    root_cause: Mapping[str, Any]
    hooks: Hooks
    client: AnthropicClient
    costs: CostCalculator | None

    @property
    def areas(self) -> Mapping[str, str]:
        return cast("Mapping[str, str]", self.root_cause.get("areas") or {})

    async def run(self, argv: Sequence[str]) -> RunResult:
        """``argv`` run in the sandbox, its time spent from the fix's budget."""
        ran = await self.runner.run(argv)
        await self.hooks.spend(0, Decimal(0), ran.seconds)
        return ran

    def gaming(self, before: dict[str, str]) -> antigaming.Context:
        ticket = self.ctx.ticket
        return antigaming.Context(
            repro_path=self.plan.repro_path, repro_test=self.plan.repro_test,
            planned=self.plan.planned, before=before,
            reported=" ".join([ticket.title, ticket.text, ticket.message or ""]),
            error_type=ticket.error_type,
        )  # fmt: skip


@asynccontextmanager
async def working(
    ctx: StageContext,
    workspace: Workspace,
    runners: Runners,
    plan: Approved,
    model: tuple[AnthropicClient, CostCalculator | None],
) -> AsyncGenerator[Job]:
    """A job in the fix's checkout (restored afterwards, whatever happens). Raises
    :class:`NoFramework` or :class:`SetupFailed` when the tests can't run."""
    root_cause = cast("Mapping[str, Any]", ctx.facts.get("root_cause") or {})
    hooks = Hooks(record=ctx.record, spend=ctx.spend, check=ctx.check)
    culprit = root_cause.get("path") or (ctx.ticket.culprit.path if ctx.ticket.culprit else None)
    async with workspace(ctx) as (tools, _history):
        try:
            setup = detect(tools.root, culprit)
            async with runners(tools.root, setup) as runner:
                yield Job(ctx, tools, runner, setup, plan, root_cause, hooks, *model)
        finally:
            await restore(tools.root)


def diff_risk(
    diff: Diff, plan: Approved, tools: ToolContext, base_areas: Mapping[str, str],
    *, reproduced: bool = True,
) -> Risk:  # fmt: skip
    """The risk of the change itself (its source files: the reproducing test isn't counted)."""
    files = [p for p in diff.files if p != plan.repro_path]
    parsed = [f for f in antigaming.parse_diff(diff.text) if f.path != plan.repro_path]
    areas = Areas(dict(base_areas))
    path_areas(files, areas)
    code_areas(((f.path, line) for f in parsed for _, line in (*f.added, *f.removed)), areas)
    blast = graph_areas(tools.graph, files, areas)
    lines = sum(f.lines_changed for f in parsed)
    return assess(areas=areas, reproduced=reproduced, files=len(files), lines=lines, blast=blast)


async def check_submission(
    job: Job,
    diff: Diff,
    *,
    gaming: antigaming.Context,
    run: Callable[[], Awaitable[tuple[RunResult, SuiteRun | None, SuiteVerdict | None]]],
) -> Checked:
    """The checks of a submission (see the module); ``run`` runs the reproducing test, then
    (when it passes) the suite, and compares it with the baseline, only once the static checks
    pass."""
    findings = tuple(antigaming.check(diff.text, gaming))
    if findings:
        return Checked(False, antigaming.summary(findings), findings)
    risk = diff_risk(diff, job.plan, job.tools, job.areas)
    if higher(risk.level, job.plan.risk):
        return Checked(False, f"The change is {risk.level.value} risk ({'; '.join(risk.reasons)})"
                       f", but the approved plan is {job.plan.risk.value} risk: keep it within "
                       "the plan (smaller, in its files).", risk=risk)  # fmt: skip
    repro, suite, verdict = await run()
    if suite is None or verdict is None:
        return Checked(False, "The reproducing test still fails.", risk=risk,
                       output=_shown(repro))  # fmt: skip
    if not verdict.passed:
        return Checked(False, verdict.reason, risk=risk, suite=suite, output=suite.tail[-SHOWN:])
    return Checked(True, verdict.reason, risk=risk, suite=suite,
                   known_failures=verdict.known_failures)  # fmt: skip


@dataclass
class _Patching:
    job: Job
    gaming: antigaming.Context
    baseline: SuiteRun
    written: list[str] = field(default_factory=list[str])
    created: set[str] = field(default_factory=set[str])
    attempts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    runs: int = 0
    accepted: tuple[Diff, Checked, str] | None = None
    blocked: Checked | None = None
    gave_up: str | None = None

    @property
    def root(self) -> Path:
        return self.job.tools.root

    @property
    def finished(self) -> bool:
        return (self.accepted is not None or self.blocked is not None or self.gave_up is not None
                or len(self.attempts) >= MAX_ATTEMPTS)  # fmt: skip

    def _writable(self, path: str) -> str | None:
        """Why ``path`` can't be written (None: it can)."""
        plan = self.job.plan
        if (
            path == plan.repro_path
            or antigaming.is_test_path(path)
            or (antigaming.is_test_config(path))
        ):
            return f"{path} is a test or test setting: a fix leaves the tests as they are."
        if path not in plan.planned:
            names = ", ".join(sorted(plan.planned))
            return f"{path} isn't in the approved plan ({names}); change only those files."
        return None

    async def call(  # noqa: PLR0911 - one case per tool
        self, name: str, args: Mapping[str, Any]
    ) -> ToolResult:
        try:
            if name == "edit_file":
                return await self.edit(str(args.get("path", "")), str(args.get("old_text", "")),
                                       str(args.get("new_text", "")))  # fmt: skip
            if name == "create_file":
                return await self.create(str(args.get("path", "")), str(args.get("content", "")))
            if name == "run_repro":
                return await self.run_one(self.job.plan.repro_path)
            if name == "run_tests":
                path = str(args.get("path", ""))
                if path not in self.job.tools.tree or not antigaming.is_test_path(path):
                    return text_result(f"{path} isn't one of the repository's test files.",
                                       "not a test file", error=True)  # fmt: skip
                return await self.run_one(path)
            if name == SUBMIT:
                return await self.submit(str(args.get("summary", "")), bool(args.get("fixed")))
        except PatchError as error:
            return text_result(str(error), str(error), error=True)
        if name in READ_TOOLS:
            return await run_tool(self.job.tools, name, args)
        return text_result(f"There is no tool {name!r}.", "unknown tool", error=True)

    async def edit(self, path: str, old: str, new: str) -> ToolResult:
        refused = self._writable(path)
        if refused:
            return text_result(refused, "not writable", error=True)
        if not old or old == new:
            return text_result("Give the exact text to replace and a different text for it.",
                               "nothing to replace", error=True)  # fmt: skip
        content = await read_text(self.root, path)
        if content is None:
            return text_result(f"{path} doesn't exist; create_file it if the plan creates it.",
                               "no such file", error=True)  # fmt: skip
        found = content.count(old)
        if found != 1:
            said = "doesn't appear" if not found else f"appears {found} times"
            return text_result(f"old_text {said} in {path}: copy one passage exactly, with "
                               "enough lines to be unique.", "no unique match",
                               error=True)  # fmt: skip
        await write_file(self.root, path, content.replace(old, new, 1))
        self._wrote(path)
        return text_result(f"Edited {path}.", f"edited {path}")

    async def create(self, path: str, content: str) -> ToolResult:
        refused = self._writable(path)
        if refused:
            return text_result(refused, "not writable", error=True)
        if path not in self.job.plan.creates:
            return text_result(f"The plan modifies {path}, it doesn't create it: use edit_file.",
                               "not a new file", error=True)  # fmt: skip
        if path not in self.created and await read_text(self.root, path) is not None:
            return text_result(f"{path} exists already.", "exists", error=True)
        await write_file(self.root, path, content)
        self.created.add(path)
        self._wrote(path)
        return text_result(f"Created {path}.", f"created {path}")

    def _wrote(self, path: str) -> None:
        if path not in self.written:
            self.written.append(path)

    async def run_one(self, path: str) -> ToolResult:
        if self.runs >= MAX_RUNS:
            return text_result(f"That was the last of {MAX_RUNS} runs: submit_patch now.",
                               "no more runs", error=True)  # fmt: skip
        self.runs += 1
        ran = await self.job.run(self.job.setup.command(path))
        said = "passed" if ran.passed else "failed"
        return text_result(f"It {said}. {_shown(ran)}", f"{path} {said}")

    async def _run_checks(self) -> tuple[RunResult, SuiteRun | None, SuiteVerdict | None]:
        job = self.job
        repro = await job.run(job.setup.command(job.plan.repro_path))
        if not repro.passed:
            return repro, None, None
        await job.hooks.check()
        suite = read_suite(job.setup.framework, await job.run(job.setup.suite()))
        return repro, suite, compare(self.baseline, suite)

    async def submit(self, summary: str, fixed: bool) -> ToolResult:
        if not fixed:
            self.gave_up = summary[:1000] or "The patch agent couldn't fix it within the plan."
            return text_result("Recorded: no fix.", "gave up")
        if not self.written:
            return text_result("Nothing is changed yet: edit the code first.", "no change",
                               error=True)  # fmt: skip
        diff = await diff_of(self.root, [*self.written, self.job.plan.repro_path])
        n = len(self.attempts) + 1
        started = time.monotonic()
        checked = await check_submission(self.job, diff, gaming=self.gaming, run=self._run_checks)
        self.attempts.append({
            "n": n, "passed": checked.passed, "blocked": checked.blocked, "reason": checked.reason,
            "output": checked.output[-SHOWN:], "summary": summary[:1000],
            "seconds": round(time.monotonic() - started, 1),
        })  # fmt: skip
        verdict = "blocked" if checked.blocked else "passed" if checked.passed else "failed"
        await self.job.hooks.record(Step("verify", f"attempt {n} of {MAX_ATTEMPTS}",
                                     f"{verdict}: {checked.reason[:200]}"))  # fmt: skip
        if checked.blocked:
            self.blocked = checked
            return text_result(f"Blocked: {checked.reason}", "blocked")
        if checked.passed:
            self.accepted = (diff, checked, summary[:1000])
            return text_result(f"Accepted: {checked.reason}", "accepted")
        left = MAX_ATTEMPTS - n
        if not left:
            return text_result(f"Failed: {checked.reason} That was the last attempt.", "failed")
        attempts = f"{left} attempt{'s' if left > 1 else ''}"
        body = (f"Attempt {n} of {MAX_ATTEMPTS} failed: {checked.reason}\n\n"
                f"<failure>\n{fenced(checked.output)}\n</failure>\n\n"
                f"Fix it and submit again ({attempts} left).")  # fmt: skip
        return text_result(body, f"attempt {n} failed", error=True)


def brief(ticket: Ticket, root_cause: Mapping[str, Any], plan: Approved,
          repro: Mapping[str, Any], baseline: SuiteRun) -> str:  # fmt: skip
    lines = [first_message(ticket), "",
             f"The root cause: {root_cause.get('claim')} ({root_cause.get('path')}:"
             f"{root_cause.get('start_line')}-{root_cause.get('end_line')})", "",
             "The approved plan (change only its files):", "<plan>", fenced(plan.plan_text),
             "</plan>", "",
             f"The reproducing test ({plan.repro_path}), failing now: make it pass.", "<test>",
             fenced(plan.repro_test), "</test>"]  # fmt: skip
    runs = cast("list[Mapping[str, Any]]", repro.get("runs") or [])
    if runs:
        lines += ["<output>", fenced(str(runs[0].get("output", ""))[-1500:]), "</output>"]
    if baseline.failed:
        lines += ["", f"Before your change, {len(baseline.failed)} of the suite's tests already "
                  "fail in the sandbox (not yours to fix)."]  # fmt: skip
    return "\n".join(lines)


def patch_stage(
    workspace: Workspace,
    runners: Runners,
    client: Callable[[], AnthropicClient],
    costs: CostCalculator | None,
) -> StageHandler:
    """The pipeline's ``patch`` handler."""

    async def handler(ctx: StageContext) -> Outcome:
        plan = approved(ctx.facts)
        if isinstance(plan, str):
            return Stop(FixStatus.FAILED, plan)
        try:
            async with working(ctx, workspace, runners, plan, (client(), costs)) as job:
                return await _patch(job)
        except (NoFramework, SetupFailed, PatchError) as error:
            return Stop(FixStatus.FAILED, str(error))

    return handler


async def _patch(job: Job) -> Outcome:
    repro = cast("Mapping[str, Any]", job.ctx.facts.get("repro") or {})
    if job.setup.framework != repro.get("framework"):
        return Stop(FixStatus.FAILED, "The project's test framework isn't the one the "
                    "reproducing test was written for.")  # fmt: skip
    ran = await job.run(job.setup.suite())
    baseline = read_suite(job.setup.framework, ran)
    said = ("passes" if baseline.ok else f"{len(baseline.failed)} failing" if baseline.readable
            else f"exit {baseline.exit_code}")  # fmt: skip
    await job.hooks.record(Step("verify", "baseline suite", said, ms=round(ran.seconds * 1000)))
    if baseline.timed_out:
        return Stop(FixStatus.FAILED, "The suite doesn't finish in the sandbox's time: a fix "
                    "can't be verified against it.")  # fmt: skip
    root = job.tools.root
    await write_file(root, job.plan.repro_path, job.plan.repro_test)
    before = {p: await read_text(root, p) or "" for p in job.plan.planned}
    patching = _Patching(job, job.gaming(before), baseline)
    ended = await Conversation(
        job.client, job.costs, job.hooks, job.tools, system_prompt(), definitions(),
        Tier.STANDARD, "medium", MAX_TURNS, REMINDER, "a submitted patch",
    ).run(brief(job.ctx.ticket, job.root_cause, job.plan, repro, baseline), patching.call,
          lambda: patching.finished)  # fmt: skip
    record = {"prompt": PROMPT_VERSION, "attempts": patching.attempts,
              "baseline": baseline.json()}  # fmt: skip
    if patching.blocked is not None:
        findings = antigaming.as_json(patching.blocked.findings)
        return Stop(FixStatus.BLOCKED, f"Anti-gaming: {patching.blocked.reason}",
                    {"patch": {**record, "findings": findings}})  # fmt: skip
    if patching.accepted is None:
        last = patching.attempts[-1]["reason"] if patching.attempts else None
        tried = f"{len(patching.attempts)} attempts; the last: {last}" if last else None
        why = patching.gave_up or tried or ended or "no change was submitted"
        return Stop(FixStatus.FAILED, f"Couldn't produce a passing fix: {why}", {"patch": record})
    diff, checked, summary = patching.accepted
    source = [f for f in antigaming.parse_diff(diff.text) if f.path != job.plan.repro_path]
    return Advance({"patch": {
        **record, "diff": diff.text, "files": [f.path for f in source],
        "lines": sum(f.lines_changed for f in source), "summary": summary,
        "risk": checked.risk.json() if checked.risk else None,
        "suite": checked.suite.json() if checked.suite else None,
    }})  # fmt: skip
