"""Answer evals (4.14): the Ask agent, run end to end on a benchmark checkout, scored on what
it answered.

The checkout is indexed as the hybrid retriever indexes it (:mod:`sensei_evals.hybrid`: its
files, symbols and chunks, the embeddings with a Voyage key, the graph), and every question
is asked of :class:`~sensei_engine.agent.loop.AskAgent` over that index, one at a time, as the
Ask page asks it (fast mode unless told otherwise; the rules route; no answer cache). Per item:

* **F0.5** of the answer's citations against the gold, by file and by line;
* **citation recall**: the gold targets a citation covers (symbol level), and **citation
  precision** two ways: ``citation_supported``, the share of citations the run's own checks
  found backing their claim (what the release gate reads), and ``citation_in_gold``, the share
  in a gold file (a floor: a right citation outside the gold counts against it);
* the **checklist score**: a judge (another model than the one answering, on the deep tier)
  reads the answer against the item's reference answer and marks each checklist fact stated,
  partly stated, missing or contradicted; the score is the weighted share stated (partly: half);
* **abstention**: the run abstained (answerable items should not, unanswerable ones should, or
  say plainly that the code doesn't do it: the checklist scores that);
* **latency**: to the first event, to the first token of the answer, and in all;
* **cost**: the agent's run, and the judge's apart.

Every model call is paid, so a run needs a budget (:class:`~sensei_evals.rerank.CostGuard`):
it is checked before each question, and a run past it stops there, with the items done scored.
Items without a reference answer are asked and scored without a checklist score.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, cast

from uuid_utils.compat import uuid7

from sensei_core.config import get_settings
from sensei_core.usage import CostCalculator
from sensei_engine.agent.loop import (
    Answer,
    AskAgent,
    Done,
    Mode,
    StepStarted,
    TextDelta,
    ToolAnswered,
    ToolCalled,
)
from sensei_engine.agent.tools.base import Cited, ToolContext
from sensei_engine.llm.anthropic_client import (
    AnthropicClient,
    LLMError,
    LLMRequest,
    LLMUsageSink,
    Tier,
)
from sensei_evals.metrics import Level, abstention_accuracy, covered, f_beta
from sensei_evals.rerank import BudgetExceeded, CostGuard
from sensei_evals.retrieval import Hit, RepoRef, RetrieverContext
from sensei_evals.runner import ItemResult, RunRecord, aggregate, percentile
from sensei_evals.schema import GoldenItem, Suite

JUDGE_MAX_TOKENS = 4_000
KEPT_CITATIONS = 20

FactVerdict = Literal["stated", "partly", "missing", "contradicted"]
CREDIT: dict[str, float] = {"stated": 1.0, "partly": 0.5, "missing": 0.0, "contradicted": 0.0}

# The release gate (4.14.4): each metric's bound, and whether the run must stay above or below.
TARGETS: dict[str, tuple[Literal[">=", "<"], float]] = {
    "citation_supported": (">=", 0.95),
    "abstention_accuracy": (">=", 0.8),
    "first_event_ms.p50": ("<", 1_000),
    "cost_usd.mean": ("<", 0.15),
    # Runs that failed (no answer) are left out of the rates above: a run with many must not
    # pass on the few that answered.
    "failed": ("<", 0.05),
    "unasked": ("<", 1),  # every item asked (a run the budget stopped isn't the gate's)
}
# First token, p50, by kind of question: a lookup (symbol, regex, conceptual) or a trace (flow).
FIRST_TOKEN_MS: dict[str, float] = {"lookup": 6_000, "trace": 15_000}
TRACE_CLASSES = frozenset({"flow"})


class Agent(Protocol):
    def run(self, question: str, ctx: ToolContext, *, mode: Mode = ...) -> AsyncIterator[Any]: ...


# Where the agent reads the index from: a tool context, on a session of its own per question.
Contexts = Callable[[], AbstractAsyncContextManager[ToolContext]]


# ---------------------------------------------------------------- one question


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    answer: Answer | None  # None: the run failed
    first_event_ms: float | None
    first_token_ms: float | None
    total_ms: float
    error: str | None = None
    tools: tuple[dict[str, Any], ...] = ()  # each call: the tool, its input, what it answered
    steps: tuple[dict[str, Any], ...] = ()  # each turn: its start (ms), whether it researched
    streamed: str | None = None  # the last answer turn's text as written, before verification

    @property
    def citations(self) -> tuple[Cited, ...]:
        return tuple(c.cited for c in self.answer.citations) if self.answer is not None else ()

    @property
    def abstained(self) -> bool:
        """The run gave an answer and abstained in it (a run that failed did neither)."""
        return self.answer is not None and self.answer.abstained is not None


async def ask(agent: Agent, question: str, ctx: ToolContext, *, mode: Mode) -> AnswerOutcome:
    """Ask once, timing the first event, the first token of the answer and the whole run. A run
    that fails is an outcome too (no answer, its error), so one bad item doesn't end the eval;
    a spent budget does (it is raised)."""
    started = time.perf_counter()
    first_event: float | None = None
    first_token: float | None = None
    answer: Answer | None = None

    def since() -> float:
        return round((time.perf_counter() - started) * 1000, 3)

    answering = False  # the open turn writes the answer (it may call no tools)
    calls: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    written: list[str] = []
    try:
        async for event in agent.run(question, ctx, mode=mode):
            if isinstance(event, ToolCalled):
                calls[event.id] = {"tool": event.name, "input": event.input}
            if isinstance(event, ToolAnswered) and event.id in calls:
                calls[event.id] |= {"summary": event.summary, "error": event.is_error}
            if first_event is None:
                first_event = since()
            if isinstance(event, StepStarted):
                answering = not event.tools
                steps.append({"at_ms": since(), "tools": event.tools})
            # The answer's first token: a turn that may call tools writes reasoning, not it.
            if isinstance(event, TextDelta) and answering and first_token is None and event.text:
                first_token = since()
            if isinstance(event, StepStarted) and not event.tools:
                written = []  # a new answer turn: the one before it was replaced
            if isinstance(event, TextDelta) and answering:
                written.append(event.text)
            if isinstance(event, Done):
                answer = event.answer
                if first_token is None and answer.text:
                    first_token = since()  # a research turn's words, given as the answer
    except BudgetExceeded:
        raise
    except (LLMError, RuntimeError, ValueError) as error:
        made = tuple(calls.values())
        return AnswerOutcome(
            None, first_event, first_token, since(), str(error), made, tuple(steps)
        )
    return AnswerOutcome(
        answer,
        first_event,
        first_token,
        since(),
        tools=tuple(calls.values()),
        steps=tuple(steps),
        streamed="".join(written) or None,
    )


# ---------------------------------------------------------------- the judge

JUDGE_SYSTEM = """\
You grade an answer to a question about a codebase against a reference answer written by \
someone who read the code. For each numbered fact of the checklist, say whether the answer \
states it ("stated"), states part of it or states it vaguely ("partly"), doesn't state it \
("missing"), or states the opposite or something incompatible ("contradicted").

Judge meaning, not wording: a fact is stated when the answer says the same thing in other words \
or names the same code (identifiers, files and routes may be quoted differently). Don't reward \
length: a fact buried in hedging that also says the opposite is contradicted. Use only the \
answer: don't credit what the reference says if the answer doesn't. Give a one-line reason for \
each verdict."""

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {
                        "type": "string",
                        "enum": ["stated", "partly", "missing", "contradicted"],
                    },
                    "why": {"type": "string"},
                },
                "required": ["n", "verdict", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Judged:
    verdicts: tuple[tuple[FactVerdict, str], ...]  # per checklist fact, in order: verdict, why
    score: float  # the weighted share stated (partly: half)
    contradicted: int


def judge_prompt(item: GoldenItem, text: str) -> str:
    if item.answer is None:
        msg = f"item {item.id} has no reference answer to judge against"
        raise ValueError(msg)
    facts = "\n".join(f"{n}. {fact.fact}" for n, fact in enumerate(item.answer.checklist, start=1))
    return (
        f"<question>\n{item.query}\n</question>\n\n"
        f"<reference_answer>\n{item.answer.text}\n</reference_answer>\n\n"
        f"<checklist>\n{facts}\n</checklist>\n\n"
        f"<answer>\n{text or '(no answer)'}\n</answer>"
    )


def read_verdicts(item: GoldenItem, data: object) -> Judged:
    """The judge's verdicts as a score. A fact the judge left out counts as missing."""
    if item.answer is None:
        msg = f"item {item.id} has no reference answer"
        raise ValueError(msg)
    given: dict[int, tuple[FactVerdict, str]] = {}
    facts: object = cast("dict[str, Any]", data).get("facts") if isinstance(data, dict) else None
    for raw in cast("list[Any]", facts) if isinstance(facts, list) else []:
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, Any]", raw)
        n, verdict = entry.get("n"), entry.get("verdict")
        if isinstance(n, int) and verdict in CREDIT:
            given[n] = (cast("FactVerdict", verdict), str(entry.get("why", "")))
    checklist = item.answer.checklist
    verdicts = tuple(given.get(n, ("missing", "not graded")) for n in range(1, len(checklist) + 1))
    total = sum(fact.weight for fact in checklist)
    earned = sum(
        fact.weight * CREDIT[verdict]
        for fact, (verdict, _) in zip(checklist, verdicts, strict=True)
    )
    return Judged(
        verdicts=verdicts,
        score=earned / total,
        contradicted=sum(verdict == "contradicted" for verdict, _ in verdicts),
    )


class Judge:
    """Grades answers on the deep tier (another model than the fast and standard tiers answer
    on), its usage counted in the run's budget."""

    def __init__(self, client: AnthropicClient, *, on_usage: LLMUsageSink | None = None) -> None:
        self.client = client
        self.on_usage = on_usage

    @property
    def model(self) -> str:
        return self.client.model_for(Tier.DEEP)

    async def judge(self, item: GoldenItem, text: str) -> Judged:
        request = LLMRequest(
            judge_prompt(item, text),
            system=JUDGE_SYSTEM,
            tier=Tier.DEEP,
            max_tokens=JUDGE_MAX_TOKENS,
            schema=JUDGE_SCHEMA,
            cache_system=True,
        )
        result = await self.client.complete(request, on_usage=self.on_usage)
        return read_verdicts(item, result.data)


# ---------------------------------------------------------------- scoring


def citation_hits(citations: Sequence[Cited]) -> list[Hit]:
    """The answer's citations as retrieval hits, in the order the answer cites them."""
    return [Hit(path=c.path, lines=(c.start_line, c.end_line)) for c in citations]


def _supported(answer: Answer | None) -> tuple[int, int] | None:
    """Of the citations the checks read, ``(backing their claim, all)``; None: not verified."""
    if answer is None or answer.verification is None:
        return None
    claims = answer.verification.claims
    cited = sum(len(c.claim.cited) for c in claims)
    backed = sum(len(c.claim.cited) for c in claims if c.verdict == "supported")
    return backed, cited


def score_answer(
    item: GoldenItem, outcome: AnswerOutcome, judged: Judged | None
) -> dict[str, float | None]:
    hits = citation_hits(outcome.citations)
    answer = outcome.answer
    scores: dict[str, float | None] = {}
    if item.answerable:
        scores["f0.5_file"] = f_beta(hits, item.gold, 0.5, "file")
        scores["f0.5_line"] = f_beta(hits, item.gold, 0.5, "line")
        found = covered(hits, item.gold, Level.SYMBOL)
        scores["citation_recall"] = len(found) / len(item.gold) if item.gold else None
        gold_files = {target.path for target in item.gold}
        scores["citation_in_gold"] = (
            sum(hit.path in gold_files for hit in hits) / len(hits) if hits else None
        )
    supported = _supported(answer)
    scores["citation_supported"] = (
        supported[0] / supported[1] if supported is not None and supported[1] else None
    )
    # A run that failed states none of the checklist (it isn't judged: nothing to read).
    failed_checklist = 0.0 if answer is None and item.answer is not None else None
    scores["checklist"] = judged.score if judged is not None else failed_checklist
    scores["contradicted"] = float(judged.contradicted) if judged is not None else None
    scores["confidence"] = (
        answer.confidence.score if answer is not None and answer.confidence else None
    )
    scores["cost_usd"] = (
        float(answer.cost_usd) if answer is not None and answer.cost_usd is not None else None
    )
    scores["turns"] = float(answer.turns) if answer is not None else None
    scores["first_event_ms"] = outcome.first_event_ms
    scores["first_token_ms"] = outcome.first_token_ms
    scores["total_ms"] = outcome.total_ms
    return scores


def _citation_json(cited: Cited) -> dict[str, Any]:
    return {"path": cited.path, "lines": [cited.start_line, cited.end_line], "score": 0.0,
            "symbol": cited.symbol_key}  # fmt: skip


def item_result(item: GoldenItem, outcome: AnswerOutcome, judged: Judged | None) -> ItemResult:
    hits = citation_hits(outcome.citations)
    found = covered(hits, item.gold, Level.SYMBOL)
    return ItemResult(
        id=item.id,
        query_class=item.class_.value,
        answerable=item.answerable,
        query=item.query,
        abstained=outcome.abstained,
        latency_ms=outcome.total_ms,
        metrics=score_answer(item, outcome, judged),
        hits=[_citation_json(c) for c in outcome.citations[:KEPT_CITATIONS]],
        missed=[t.model_dump(mode="json", exclude_none=True) for t in item.gold if t not in found],
        answer=outcome.answer.text if outcome.answer is not None else None,
        verdicts=(
            [{"verdict": verdict, "why": why} for verdict, why in judged.verdicts]
            if judged is not None
            else None
        ),
        error=outcome.error,
        tools=list(outcome.tools) or None,
        claims=_claims(outcome.answer),
        steps=list(outcome.steps) or None,
        streamed=(
            outcome.streamed
            if outcome.answer is not None and outcome.streamed != outcome.answer.text
            else None
        ),
    )


def _claims(answer: Answer | None) -> list[dict[str, Any]] | None:
    """The answer's claims as the support check judged them (None: not verified)."""
    if answer is None or answer.verification is None:
        return None
    return [
        {
            "text": checked.claim.text,
            "verdict": checked.verdict,
            "cites": [f"{c.path}:{c.start_line}-{c.end_line}" for c in checked.claim.cited],
        }
        for checked in answer.verification.claims
    ]


# ---------------------------------------------------------------- the gate


@dataclass(frozen=True, slots=True)
class Miss:
    metric: str
    value: float | None
    bound: str  # what it should be, e.g. ">= 0.95"


def _latencies(results: Sequence[ItemResult], key: str) -> list[float]:
    return [v for r in results if (v := r.metrics.get(key)) is not None]


def answer_aggregate(results: Sequence[ItemResult]) -> dict[str, Any]:
    """The runner's averages (overall and by class), with the answer's own latencies and cost:
    p50 and p95 of the first event and first token (lookups and traces apart), the mean cost."""
    metrics = aggregate(results)
    # Abstention is judged on the runs that answered: a run that failed neither answered nor
    # abstained, so it would count as abstaining right on an unanswerable item.
    answered = [r for r in results if not r.metrics.get("failed")]
    metrics["overall"]["abstention_accuracy"] = abstention_accuracy(
        (r.answerable, r.abstained) for r in answered
    )
    for name, scores in metrics["by_class"].items():
        scores["abstention_accuracy"] = abstention_accuracy(
            (r.answerable, r.abstained) for r in answered if r.query_class == name
        )
    lookups = [r for r in results if r.query_class not in TRACE_CLASSES]
    traces = [r for r in results if r.query_class in TRACE_CLASSES]
    costs = _latencies(results, "cost_usd")
    metrics["answer"] = {
        "first_event_ms": {
            "p50": percentile(_latencies(results, "first_event_ms"), 0.5),
            "p95": percentile(_latencies(results, "first_event_ms"), 0.95),
        },
        "first_token_ms": {
            kind: {
                "p50": percentile(_latencies(group, "first_token_ms"), 0.5),
                "p95": percentile(_latencies(group, "first_token_ms"), 0.95),
            }
            for kind, group in (("lookup", lookups), ("trace", traces))
        },
        "total_ms": {
            "p50": percentile(_latencies(results, "total_ms"), 0.5),
            "p95": percentile(_latencies(results, "total_ms"), 0.95),
        },
        "cost_usd": {"mean": sum(costs) / len(costs) if costs else None, "sum": sum(costs)},
    }
    return metrics


def gate(metrics: dict[str, Any]) -> list[Miss]:
    """What the run misses of the release gate's targets (an unmeasured one is a miss: the gate
    passes on what was shown, not on what wasn't)."""
    overall: dict[str, Any] = metrics.get("overall", {})
    answer: dict[str, Any] = metrics.get("answer", {})
    values: dict[str, float | None] = {
        "citation_supported": overall.get("citation_supported"),
        "abstention_accuracy": overall.get("abstention_accuracy"),
        "first_event_ms.p50": answer.get("first_event_ms", {}).get("p50"),
        "cost_usd.mean": answer.get("cost_usd", {}).get("mean"),
        "failed": overall.get("failed"),
        "unasked": overall.get("unasked", 0.0),
    }
    for kind in FIRST_TOKEN_MS:
        values[f"first_token_ms.{kind}.p50"] = (
            answer.get("first_token_ms", {}).get(kind, {}).get("p50")
        )
    targets = {
        **TARGETS,
        **{f"first_token_ms.{k}.p50": ("<", v) for k, v in FIRST_TOKEN_MS.items()},
    }
    misses: list[Miss] = []
    for name, (op, bound) in targets.items():
        value = values.get(name)
        ok = value is not None and (value >= bound if op == ">=" else value < bound)
        if not ok:
            misses.append(Miss(name, value, f"{op} {bound:g}"))
    return misses


# ---------------------------------------------------------------- the run


async def run_answers(
    suite: Suite,
    items: Sequence[GoldenItem],
    *,
    agent: Agent,
    contexts: Contexts,
    judge: Judge | None,
    guard: CostGuard,
    repo: RepoRef,
    mode: Mode = Mode.FAST,
    config: dict[str, Any] | None = None,
    git_sha: str | None = None,
) -> RunRecord:
    """Ask every item (in order, one at a time), judge its answer and score it; stop at the
    budget, with what was done."""
    results: list[ItemResult] = []
    stopped: str | None = None
    for item in items:
        try:
            guard.check()
            async with contexts() as ctx:
                outcome = await ask(agent, item.query, ctx, mode=mode)
            judged = None
            # A run that failed answered nothing to grade (it scores 0 as it is): no judge paid.
            if judge is not None and item.answer is not None and outcome.answer is not None:
                try:
                    judged = await judge.judge(item, outcome.answer.text)
                except LLMError as error:
                    outcome = AnswerOutcome(
                        outcome.answer, outcome.first_event_ms, outcome.first_token_ms,
                        outcome.total_ms, error=f"judge: {error}", tools=outcome.tools,
                        steps=outcome.steps, streamed=outcome.streamed,
                    )  # fmt: skip
        except BudgetExceeded as error:
            stopped = str(error)
            break
        result = item_result(item, outcome, judged)
        result.metrics["failed"] = 1.0 if outcome.answer is None else 0.0  # its mean: the rate
        results.append(result)
    metrics = answer_aggregate(results)
    # A run the budget stopped wasn't the suite: what it left unasked keeps the gate shut.
    metrics["overall"]["unasked"] = float(len(items) - len(results))
    metrics["gate"] = [
        {"metric": m.metric, "value": m.value, "bound": m.bound} for m in gate(metrics)
    ]
    metrics["spent_usd"] = str(guard.spent)
    return RunRecord(
        id=uuid7(),
        suite=suite.name,
        suite_version=suite.version,
        retriever=f"ask:{mode.value}",
        config={
            "repo": repo.id,
            "mode": mode.value,
            "judge": judge.model if judge is not None else None,
            "budget_usd": str(guard.budget),
            "stopped": stopped,
            "asked": len(results),
            **(config or {}),
        },
        repo_sha=repo.sha,
        metrics=metrics,
        per_item=results,
        created_at=datetime.now(UTC),
        git_sha=git_sha,
    )


async def evaluate(
    suite: Suite,
    items: Sequence[GoldenItem],
    repo: RepoRef,
    *,
    options: dict[str, str],
    budget: Decimal,
    mode: Mode = Mode.FAST,
    judge: bool = True,
    config: dict[str, Any] | None = None,
    git_sha: str | None = None,
) -> tuple[RunRecord, Decimal]:
    """``sensei-eval answers``: index the checkout (as the hybrid retriever does, with its
    ``options``), ask every item within ``budget`` and score it. Returns the run and what it
    spent. Raises ``ValueError`` for options it can't run with."""
    from sensei_evals.hybrid import checkout_hybrid  # noqa: PLC0415 - imports the index's stack

    if "rerank" in options:
        msg = "the answer eval reranks as the agent does; leave --opt rerank out"
        raise ValueError(msg)
    settings = get_settings()
    retriever = checkout_hybrid(RetrieverContext(suite=suite, repo=repo, options=options))
    calculator = CostCalculator.from_settings(settings.pricing)
    guard = CostGuard(budget, calculator)
    client = AnthropicClient(settings.anthropic)
    agent = AskAgent(
        client, costs=calculator, on_usage=guard.llm_usage, on_rerank_usage=guard.rerank_usage
    )
    graded = Judge(client, on_usage=guard.llm_usage) if judge else None
    try:
        await retriever.prepare(repo)
        record = await run_answers(
            suite, items, agent=agent, contexts=retriever.agent_context, judge=graded,
            guard=guard, repo=repo, mode=mode, config=config, git_sha=git_sha,
        )  # fmt: skip
    finally:
        await retriever.aclose()
    return record, guard.spent
