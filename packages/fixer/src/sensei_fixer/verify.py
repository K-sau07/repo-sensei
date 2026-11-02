"""Verification (8.7): the patch, as it will be proposed, checked from a clean checkout.

The stored diff is applied to the commit being fixed (exactly what the pull request will hold,
not the patch agent's working copy), then:

1. the anti-gaming checks again (:mod:`.antigaming`): a hit blocks the run;
2. the reproducing test passes in every one of :data:`RUNS` runs;
3. the whole suite passes against its baseline (:mod:`.suites`);
4. a **verifier** model (Opus) reads the root cause, the plan, the test and the diff, writes
   extra tests on the changed lines (other inputs, the behaviour that must not change: they
   must pass with the fix), and gives its verdict: whether the fix is **test-shaped** (makes the
   test pass without fixing the bug), its concerns, and what it couldn't verify. Test-shaped
   blocks the run; no verdict fails it (nothing vouches for it);
5. the change's **risk** (:mod:`.risk`), which may not be above the approved plan's (that
   blocks it: people approved less), and its **merge readiness**, each with its reasons.

Any failure of 2-3 is "couldn't produce a passing fix". The extra tests are evidence, not part
of the fix; the checkout is restored afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, cast

from sensei_core.fixes.tickets import Ticket
from sensei_core.models import FixStatus
from sensei_core.usage import CostCalculator
from sensei_engine.agent.tools import BY_NAME, run_tool
from sensei_engine.agent.tools.base import ToolResult
from sensei_engine.llm.anthropic_client import AnthropicClient, Tier
from sensei_fixer import antigaming
from sensei_fixer.agents import Conversation
from sensei_fixer.frameworks import NoFramework
from sensei_fixer.investigate import READ_TOOLS, fenced, first_message
from sensei_fixer.patch import Approved, Job, approved, diff_risk, working
from sensei_fixer.patching import Diff, PatchError, apply, read_text, write_file
from sensei_fixer.reproduce import RUNS, Runners, Workspace
from sensei_fixer.risk import higher, readiness
from sensei_fixer.runs import RunResult, SetupFailed
from sensei_fixer.stages import Advance, Outcome, StageContext, StageHandler, Step, Stop
from sensei_fixer.suites import SuiteRun, SuiteVerdict, compare, read_suite
from sensei_fixer.tooling import text_result

PROMPT_VERSION = "verify.v1"
MAX_TURNS = 16
MAX_TRIES = 6  # run_extra_tests calls
MAX_TEST_BYTES = 20_000
SHOWN = 2_500
SUBMIT = "submit_verdict"
REMINDER = (
    "Finish with submit_verdict: whether the fix is test-shaped, why, your concerns and what "
    "you couldn't verify. Write and run extra tests first if you haven't."
)
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "write_extra_tests",
        "description": "Write the extra tests' file (its whole content; the file and its place "
        "are fixed): tests of the changed code's other behaviour, which must pass with the fix.",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_extra_tests",
        "description": "Run the extra tests once in the sandbox, with the fix: exit and output.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": SUBMIT,
        "description": "Your verdict on the fix: test_shaped true if it makes the reproducing "
        "test pass without fixing the reported bug (it matches the test's values, hides the "
        "error, or detects the test); why; your concerns about it (none: empty); what you "
        "couldn't verify.",
        "input_schema": {
            "type": "object",
            "properties": {
                "test_shaped": {"type": "boolean"},
                "reason": {"type": "string"},
                "concerns": {"type": "array", "items": {"type": "string"}},
                "unverified": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["test_shaped", "reason", "concerns", "unverified"],
            "additionalProperties": False,
        },
    },
)


def system_prompt() -> str:
    found = resources.files("sensei_fixer").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return found.read_text(encoding="utf-8")


def definitions() -> list[dict[str, Any]]:
    return [*(BY_NAME[name].definition() for name in READ_TOOLS), *TOOLS]


def _strings(value: Any, limit: int = 10) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v)[:500] for v in cast("list[Any]", value) if str(v).strip()][:limit]


@dataclass(frozen=True, slots=True)
class Verdict:
    test_shaped: bool
    reason: str
    concerns: tuple[str, ...]
    unverified: tuple[str, ...]


@dataclass
class _Verifying:
    job: Job
    extra_path: str
    content: str | None = None
    tries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    verdict: Verdict | None = None

    async def call(  # noqa: PLR0911 - one case per tool
        self, name: str, args: Mapping[str, Any]
    ) -> ToolResult:
        if name == "write_extra_tests":
            content = str(args.get("content", ""))
            if not content.strip() or len(content.encode()) > MAX_TEST_BYTES:
                return text_result(f"A test file of 1 to {MAX_TEST_BYTES} bytes.", "bad tests",
                                   error=True)  # fmt: skip
            await write_file(self.job.tools.root, self.extra_path, content)
            self.content = content
            return text_result(f"Written to {self.extra_path}.", "wrote extra tests")
        if name == "run_extra_tests":
            if self.content is None:
                return text_result("Write them first (write_extra_tests).", "no tests", error=True)
            if len(self.tries) >= MAX_TRIES:
                return text_result(f"That was the last of {MAX_TRIES} runs: submit_verdict now.",
                                   "no more runs", error=True)  # fmt: skip
            ran = await self.run()
            said = "passed" if ran.passed else "failed"
            return text_result(f"They {said}. Exit {ran.exit_code}.\n\n{ran.output[-SHOWN:]}",
                               f"extra tests {said}")  # fmt: skip
        if name == SUBMIT:
            self.verdict = Verdict(
                test_shaped=bool(args.get("test_shaped")),
                reason=str(args.get("reason") or "")[:1000],
                concerns=tuple(_strings(args.get("concerns"))),
                unverified=tuple(_strings(args.get("unverified"))),
            )
            return text_result("Verdict recorded.", "verdict")
        if name in READ_TOOLS:
            return await run_tool(self.job.tools, name, args)
        return text_result(f"There is no tool {name!r}.", "unknown tool", error=True)

    async def run(self) -> RunResult:
        ran = await self.job.run(self.job.setup.command(self.extra_path))
        self.tries.append({"passed": ran.passed, "exit_code": ran.exit_code,
                           "output": ran.output[-SHOWN:]})  # fmt: skip
        return ran


def brief(ticket: Ticket, root_cause: Mapping[str, Any], plan: Approved, diff: str,
          extra_path: str) -> str:  # fmt: skip
    return "\n".join([
        first_message(ticket), "",
        f"The root cause: {root_cause.get('claim')} ({root_cause.get('path')}:"
        f"{root_cause.get('start_line')}-{root_cause.get('end_line')})", "",
        "The approved plan:", "<plan>", fenced(plan.plan_text), "</plan>", "",
        f"The reproducing test ({plan.repro_path}): it failed before the fix and passes with it.",
        "<test>", fenced(plan.repro_test), "</test>", "",
        "The fix (applied in the checkout you read):", "<diff>", fenced(diff), "</diff>", "",
        f"Your extra tests' file: {extra_path} (write_extra_tests sets its content).",
    ])  # fmt: skip


def verify_stage(
    workspace: Workspace,
    runners: Runners,
    client: Callable[[], AnthropicClient],
    costs: CostCalculator | None,
) -> StageHandler:
    """The pipeline's ``verify`` handler."""

    async def handler(ctx: StageContext) -> Outcome:
        plan = approved(ctx.facts)
        patch = cast("Mapping[str, Any] | None", ctx.facts.get("patch"))
        if isinstance(plan, str):
            return Stop(FixStatus.FAILED, plan)
        if not patch or not patch.get("diff"):
            return Stop(FixStatus.FAILED, "There is no patch to verify.")
        try:
            async with working(ctx, workspace, runners, plan, (client(), costs)) as job:
                tested = await _tests_pass(job, patch)
                if isinstance(tested, Stop):
                    return tested
                return await _judged(job, str(patch["diff"]), *tested)
        except (NoFramework, SetupFailed, PatchError) as error:
            return Stop(FixStatus.FAILED, f"Verification: {error}")

    return handler


async def _tests_pass(
    job: Job, patch: Mapping[str, Any]
) -> Stop | tuple[dict[str, Any], SuiteRun, SuiteVerdict]:
    """The stored diff applied and checked, the reproducing test run :data:`RUNS` times, the
    suite against its baseline: what they showed, or why the fix stops."""
    text, plan, root = str(patch["diff"]), job.plan, job.tools.root
    files = [f.path for f in antigaming.parse_diff(text) if f.path != plan.repro_path]
    before = {p: await read_text(root, p) or "" for p in files}
    await apply(root, text)
    findings = antigaming.check(text, job.gaming(before))
    if findings:
        return Stop(FixStatus.BLOCKED, f"Anti-gaming: {antigaming.summary(findings)}",
                    {"verification": {"findings": antigaming.as_json(findings)}})  # fmt: skip
    record: dict[str, Any] = {"prompt": PROMPT_VERSION}
    runs: list[RunResult] = []
    for n in range(1, RUNS + 1):
        await job.hooks.check()
        ran = await job.run(job.setup.command(plan.repro_path))
        await job.hooks.record(Step("verify", f"reproducing test, run {n} of {RUNS}",
                                    "passed" if ran.passed else "failed"))  # fmt: skip
        runs.append(ran)
    record["repro_runs"] = [{"passed": r.passed, "exit_code": r.exit_code,
                             "output": r.output[-SHOWN:]} for r in runs]  # fmt: skip
    passing = sum(r.passed for r in runs)
    if passing < RUNS:
        return Stop(FixStatus.FAILED, f"Couldn't produce a passing fix: with the patch, the "
                    f"reproducing test passed in {passing} of {RUNS} runs.",
                    {"verification": record})  # fmt: skip
    await job.hooks.check()
    ran = await job.run(job.setup.suite())
    suite = read_suite(job.setup.framework, ran)
    baseline = SuiteRun.from_json(cast("Mapping[str, Any]", patch.get("baseline") or {}))
    verdict = compare(baseline, suite)
    record["suite"] = {**suite.json(), "verdict": verdict.reason}
    await job.hooks.record(Step("verify", "whole suite", verdict.reason[:200],
                                ms=round(ran.seconds * 1000)))  # fmt: skip
    if not verdict.passed:
        return Stop(FixStatus.FAILED, f"Couldn't produce a passing fix: {verdict.reason}",
                    {"verification": record})  # fmt: skip
    return record, suite, verdict


async def _judged(
    job: Job, text: str, record: dict[str, Any], suite: SuiteRun, suite_verdict: SuiteVerdict
) -> Outcome:
    """The verifier's verdict and extra tests, then the change's risk and merge readiness."""
    extra_path = job.setup.extra_path(job.ctx.fix_id.hex[:12])
    verifying = _Verifying(job, extra_path)
    ended = await Conversation(
        job.client, job.costs, job.hooks, job.tools, system_prompt(), definitions(), Tier.DEEP,
        "high", MAX_TURNS, REMINDER, "a verdict",
    ).run(brief(job.ctx.ticket, job.root_cause, job.plan, text, extra_path), verifying.call,
          lambda: verifying.verdict is not None)  # fmt: skip
    verdict = verifying.verdict
    if verdict is None:
        return Stop(FixStatus.FAILED, f"Verification: the verifier gave no verdict ({ended}).",
                    {"verification": record})  # fmt: skip
    extra_failed = False
    if verifying.content is not None:
        extra_failed = not (await verifying.run()).passed  # the file as it was left, once more
    record.update({
        "verdict": {"test_shaped": verdict.test_shaped, "reason": verdict.reason,
                    "concerns": list(verdict.concerns), "unverified": list(verdict.unverified)},
        "extra_tests": {"path": extra_path, "content": verifying.content, "tries": verifying.tries,
                        "passed": None if verifying.content is None else not extra_failed},
    })  # fmt: skip
    if verdict.test_shaped:
        return Stop(FixStatus.BLOCKED, "Anti-gaming: the verifier found the fix test-shaped: "
                    f"{verdict.reason}", {"verification": record})  # fmt: skip
    files = tuple(f.path for f in antigaming.parse_diff(text))
    risk = await asyncio.to_thread(diff_risk, Diff(text, files), job.plan, job.tools, job.areas)
    record["risk"] = risk.json()
    if higher(risk.level, job.plan.risk):
        return Stop(FixStatus.BLOCKED, f"The fix is {risk.level.value} risk "
                    f"({'; '.join(risk.reasons)}), above the {job.plan.risk.value} risk people "
                    "approved.", {"verification": record})  # fmt: skip
    blast = cast("Mapping[str, Any]", job.root_cause.get("blast_radius") or {})
    counts = cast("Mapping[str, int]", blast.get("counts") or {})
    covered = (counts.get("tests", 0) > 0) if blast else None
    # Failing before the fix too (or exiting non-zero, as before it, naming none).
    known = len(suite_verdict.known_failures) or (0 if suite.ok else 1)
    ready = readiness(
        risk=risk.level, known_failures=known, concerns=verdict.concerns, covered=covered,
        extra_failed=extra_failed, extra_written=verifying.content is not None,
    )  # fmt: skip
    unverified = list(verdict.unverified)
    if known:
        unverified.append(f"{known} test(s) that already failed in the sandbox before the fix")
    if covered is False:
        unverified.append("no existing test reaches the root cause's code (the graph)")
    elif covered is None:
        unverified.append("which existing tests reach the changed code (no graph yet)")
    record.update({"readiness": ready.json(), "unverified": unverified})
    await job.hooks.record(Step("verify", "merge readiness", f"{ready.score}/5: {ready.reason}"))
    return Advance({"verification": record, "risk": risk.level, "merge_readiness": ready.score})
