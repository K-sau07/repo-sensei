"""The Ask agent's loop (4.2): a question answered with the tools, in a few bounded turns.

* **Model and effort**, by what the question is (the query router's rules): Sonnet at effort
  ``low`` for a lookup (a name, a string), ``medium`` for the rest; **Opus** at ``high`` for a
  question that cuts across the repository (its architecture, a change's impact), in deep mode,
  or when the caller escalates (a verifier that found the answer short, 4.5).
* **At most** :attr:`Budget.max_turns` **turns**: research, then the answer (and
  :attr:`Budget.max_gap_turns` more for what verification finds missing). In research the
  model calls tools as it likes (``tool_choice`` auto; it is told to call independent ones
  together), every result of a turn going back in one message, until it commits to its
  evidence (``submit_evidence``; one reminder if it answers without), or its turns or budget
  (tokens, dollars) run out.
* **The answer** (4.4) is written in a turn of its own, from only the final evidence: what the
  agent committed to, then the best of the rest of what it read, reranked
  (:mod:`.evidence`); no tools, and a note when the research was cut short.
* **Round 0** (:mod:`.round0`): the question's search results, and its flow traced, go into
  the first turn, so most answers start with their evidence.
* **Cached**: the tools and the system prompt (instructions, then the repository's overview)
  are one stable prefix, the conversation's tail cached as it grows (:meth:`AnthropicClient.
  turn`); :meth:`AskAgent.prewarm` writes the prefix before the first question.
* **Events** (:data:`AgentEvent`), as they happen: a turn starting, each tool call and its
  result, the answer's text as it streams, its citations, and the answer. The API streams them
  (4.7) and records them (4.8).

Tools run one at a time, in the order the model called them: they share the request's
database session, which runs one statement at a time.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import resources
from typing import Any, Literal

from sensei_core.usage import CostCalculator
from sensei_engine.agent.confidence import Abstention, Confidence, abstention, confidence
from sensei_engine.agent.evidence import answer_blocks, final_set, gather
from sensei_engine.agent.overview import repo_overview
from sensei_engine.agent.round0 import pre_retrieve
from sensei_engine.agent.tools import CitationIndex, ToolContext, definitions, run_tool
from sensei_engine.agent.tools.base import Cited, Evidence, Trail
from sensei_engine.agent.verify import (
    MAX_ROUNDS,
    Checked,
    Verification,
    claims,
    existing,
    identifiers,
    support,
    unknown,
    whole,
)
from sensei_engine.llm.anthropic_client import (
    PROVIDER,
    AnthropicClient,
    Effort,
    LLMTurn,
    LLMTurnResult,
    LLMUsage,
    LLMUsageSink,
    Tier,
)
from sensei_engine.search.layers import lane_of
from sensei_engine.search.rerank import Reranker, RerankUsageSink
from sensei_engine.search.route import QueryClass, Route, classify

PROMPT_VERSION = "ask_system.v1"
LOOKUPS = frozenset({QueryClass.SYMBOL, QueryClass.REGEX})
CROSS_CUTTING = frozenset({QueryClass.ARCHITECTURE, QueryClass.IMPACT})
LAST_TURN_NOTE = (
    "Your research reached its last turn before you committed to evidence: answer from what "
    "is here, and say what you couldn't check."
)
BUDGET_NOTE = (
    "Your research used its budget before you committed to evidence: answer from what is "
    "here, and say what you couldn't check."
)
GAP_TURNS = 2  # the turns a search for what an answer misses may take
GAP_NOTE = (
    "Your answer doesn't cover all of the question yet: {gap} Search for it, then call "
    "submit_evidence again with what you found."
)
EVIDENCE_REMINDER = (
    "Before you answer, call submit_evidence with the code you will cite. The answer is "
    "written from that evidence."
)


NO_RESULT = "a turn ended without its result"


class Mode(enum.StrEnum):
    FAST = "fast"
    DEEP = "deep"


@dataclass(frozen=True, slots=True)
class Budget:
    """What one run may spend: turns, tokens (input counts what was read from the cache too)
    and dollars (None: no cap, or no price for the model)."""

    max_turns: int = 5
    max_gap_turns: int = 4  # more, for what verification finds missing (answers included)
    max_input_tokens: int = 600_000
    max_output_tokens: int = 48_000
    max_cost_usd: Decimal | None = Decimal("0.50")


@dataclass(frozen=True, slots=True)
class Exchange:
    """A question asked earlier in the conversation, and its answer's text."""

    question: str
    answer: str


# ---------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class StepStarted:
    turn: int
    model: str
    effort: Effort | None
    tools: bool  # whether the model may call tools this turn


@dataclass(frozen=True, slots=True)
class PreRetrieved:
    """Round 0 (4.3): what the question started with."""

    results: int
    files: int
    traced: int
    ms: int
    agreement: float | None = None  # of its two passes (flow, architecture)


@dataclass(frozen=True, slots=True)
class EvidenceChosen:
    """The evidence the answer is written from (4.4): how many pieces, how many of them the
    agent submitted."""

    pieces: int
    submitted: int


@dataclass(frozen=True, slots=True)
class Verified:
    """The answer's checks (4.5)."""

    verification: Verification


@dataclass(frozen=True, slots=True)
class GapFound:
    """The answer isn't whole: what it misses, searched for once more."""

    missing: tuple[str, ...]
    gap: str


@dataclass(frozen=True, slots=True)
class ToolCalled:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolAnswered:
    id: str
    name: str
    summary: str
    hits: int
    is_error: bool
    ms: int
    content: tuple[dict[str, Any], ...] = ()  # what the model was given (for the run's record)
    card: dict[str, Any] | None = None  # what it found, for the page to draw (agent.cards)


@dataclass(frozen=True, slots=True)
class TextDelta:
    turn: int
    text: str


@dataclass(frozen=True, slots=True)
class AnswerCitation:
    """A span of the answer's text and the lines that back it."""

    start: int  # in the answer's text
    end: int
    cited: Cited
    quoted: str  # the cited text, as the model saw it


@dataclass(frozen=True, slots=True)
class CitationFound:
    n: int  # its place among the answer's citations
    citation: AnswerCitation


Stop = Literal["answered", "turns", "budget", "truncated", "refused"]
# The stop reasons of an answer that isn't whole: what the run says it stopped by.
CUT_SHORT: dict[str, Stop] = {"max_tokens": "truncated", "refusal": "refused"}
STOPPED_NOTES: dict[Stop, str] = {"turns": LAST_TURN_NOTE, "budget": BUDGET_NOTE}


@dataclass(frozen=True, slots=True)
class Answer:
    text: str
    citations: tuple[AnswerCitation, ...]
    stopped: Stop  # answered, or why it stopped short
    model: str
    turns: int
    usage: tuple[LLMUsage, ...]
    cost_usd: Decimal | None  # None: a model without a price
    evidence: tuple[Evidence, ...]
    escalated: bool
    dropped_citations: int = 0  # citations that point at nothing the run gave, or no lines
    verification: Verification | None = None  # None: not verified
    confidence: Confidence | None = None  # None: not verified
    abstained: Abstention | None = None  # why it is too thin to give, when it is
    searched: tuple[str, ...] = ()  # what the run searched for, in order


@dataclass(frozen=True, slots=True)
class Done:
    answer: Answer


AgentEvent = (
    PreRetrieved
    | StepStarted
    | ToolCalled
    | ToolAnswered
    | EvidenceChosen
    | Verified
    | GapFound
    | TextDelta
    | CitationFound
    | Done
)


# ---------------------------------------------------------------- the loop


def system_prompt() -> str:
    path = resources.files("sensei_engine.agent").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Plan:
    """The model and effort a question runs on."""

    tier: Tier
    effort: Effort
    escalated: bool


def plan_for(route: Route, mode: Mode, *, escalate: bool = False) -> Plan:
    if mode is Mode.DEEP or escalate or route.query_class in CROSS_CUTTING:
        return Plan(Tier.DEEP, "high", escalated=True)
    return Plan(Tier.STANDARD, "low" if route.query_class in LOOKUPS else "medium", False)


@dataclass(slots=True)
class _Spent:
    budget: Budget
    costs: CostCalculator | None
    usage: list[LLMUsage] = field(default_factory=list[LLMUsage])
    cost: Decimal | None = Decimal(0)

    def add(self, usage: Sequence[LLMUsage]) -> None:
        self.usage.extend(usage)
        for item in usage:
            if self.costs is None or self.cost is None:
                self.cost = None
                continue
            priced = self.costs.cost(
                PROVIDER,
                item.model,
                tokens_in=item.input_tokens,
                tokens_out=item.output_tokens,
                tokens_cache_read=item.cache_read_tokens,
                tokens_cache_write=item.cache_write_tokens,
            )
            self.cost = None if priced is None else self.cost + priced

    @property
    def over(self) -> bool:
        tokens_in = sum(
            u.input_tokens + u.cache_read_tokens + u.cache_write_tokens for u in self.usage
        )
        tokens_out = sum(u.output_tokens for u in self.usage)
        cap = self.budget.max_cost_usd
        return (
            tokens_in >= self.budget.max_input_tokens
            or tokens_out >= self.budget.max_output_tokens
            or (cap is not None and self.cost is not None and self.cost >= cap)
        )


@dataclass(slots=True)
class _Run:
    """One run's state, as its phases (round 0, research, answer) leave it."""

    plan: Plan
    model: str
    system: tuple[str, str]
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]]  # the research's conversation
    read: CitationIndex  # every search result the research was shown, in order
    spent: _Spent
    stopped: Stop = "answered"
    result: LLMTurnResult | None = None  # the last turn's
    answered: LLMTurnResult | None = None  # the turn the answer is (an answer turn's, else)
    index: CitationIndex | None = None  # what the answer's citations count (its evidence)
    turn: int = 0
    evidence_blocks: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    shown: str = ""  # the answer turn's evidence, as text (what its names may come from)
    text: str = ""  # the answer, read from its turn
    citations: tuple[AnswerCitation, ...] = ()
    dropped: int = 0
    verification: Verification | None = None
    agreement: float | None = None  # of round 0's two passes
    trail: Trail | None = None  # the last trace's best path (round 0's, a tool's)
    searched: list[str] = field(default_factory=list[str])


class AskAgent:
    def __init__(
        self,
        client: AnthropicClient,
        *,
        budget: Budget | None = None,
        costs: CostCalculator | None = None,
        on_usage: LLMUsageSink | None = None,
        pre_retrieval: bool = True,
        reranker: Reranker | None = None,
        on_rerank_usage: RerankUsageSink | None = None,
        verify: bool = True,
    ) -> None:
        self.client = client
        self.budget = budget or Budget()
        self.costs = costs
        self.on_usage = on_usage
        self.pre_retrieval = pre_retrieval
        self.reranker = reranker  # 4.4's final rerank (None: the evidence in reading order)
        self.on_rerank_usage = on_rerank_usage
        self.verify = verify  # 4.5's checks (False: the answer as it came)

    def _counted(self, run: _Run) -> LLMUsageSink:
        """Where a check's usage goes (the support check, the whole-answer check, the evidence
        pick): to the run's budget, as its turns' does, and on to ``on_usage``."""

        async def sink(used: LLMUsage) -> None:
            run.spent.add([used])
            if self.on_usage is not None:
                await self.on_usage(used)

        return sink

    async def prewarm(self, ctx: ToolContext, *, overview: str | None = None) -> None:
        """Write the run's shared prefix to the cache (an Ask page opening): Sonnet's, at the
        effort most questions run at."""
        system = (system_prompt(), overview or await repo_overview(ctx))
        request = LLMTurn((), system, definitions(), Tier.STANDARD, effort="medium")
        await self.client.prewarm(request, on_usage=self.on_usage)

    async def run(
        self,
        question: str,
        ctx: ToolContext,
        *,
        conversation: Sequence[Exchange] = (),
        mode: Mode = Mode.FAST,
        overview: str | None = None,
        escalate: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        route = classify(question).route
        plan = plan_for(route, mode, escalate=escalate)
        run = _Run(
            plan,
            self.client.model_for(plan.tier),
            (system_prompt(), overview or await repo_overview(ctx)),
            definitions(),
            _history(conversation, question),
            CitationIndex(),
            _Spent(self.budget, self.costs),
        )
        if self.pre_retrieval and (found := await _round0(ctx, question, run)):
            yield found
        async for event in self._research(run, ctx, self.budget.max_turns - 1):
            yield event
        async for event in self._answer(run, ctx, question, conversation):
            yield event
        layers = tuple(dict.fromkeys(lane_of(layer).value for layer in route.layers))
        async for event in self._verified(run, ctx, question, conversation, layers):
            yield event
        result = run.result
        if result is None:
            raise RuntimeError(NO_RESULT)
        for n, citation in enumerate(run.citations, start=1):
            yield CitationFound(n, citation)
        sure = confidence(run.verification, run.trail) if self.verify else None
        thin = (
            abstention(
                sure,
                run.verification,
                run.trail,
                run.searched,
                flow=route.query_class is QueryClass.FLOW,
            )
            if sure is not None
            else None
        )
        yield Done(
            Answer(
                run.text,
                run.citations,
                CUT_SHORT.get(result.stop_reason or "", run.stopped),
                result.model,
                run.turn,
                tuple(run.spent.usage),
                run.spent.cost if self.costs is not None else None,
                tuple(ctx.evidence),
                plan.escalated,
                run.dropped,
                run.verification,
                sure,
                thin,
                tuple(run.searched),
            )
        )

    async def _verified(
        self,
        run: _Run,
        ctx: ToolContext,
        question: str,
        conversation: Sequence[Exchange],
        layers: tuple[str, ...],
    ) -> AsyncIterator[AgentEvent]:
        """The answer read from its turn, then verified (4.5); one that isn't whole searched
        for what it misses and answered again, at most :data:`~.verify.MAX_ROUNDS` times."""
        gap_left = self.budget.max_gap_turns
        for rounds in range(MAX_ROUNDS + 1):
            answered = run.answered
            if answered is None:
                raise RuntimeError(NO_RESULT)
            index = run.index if run.index is not None else run.read
            run.text, run.citations, run.dropped = _answer(answered.content, index)
            if not self.verify or answered.stop_reason in CUT_SHORT:
                return
            run.verification = await self._verify(run, ctx, question, layers, rounds)
            yield Verified(run.verification)
            whole_answer = run.verification.whole
            allowed = min(GAP_TURNS, gap_left - 1)  # a search turn at least, and its answer's
            if whole_answer is None or whole_answer.complete or rounds == MAX_ROUNDS:
                return
            if allowed < 1 or run.spent.over:
                return
            yield GapFound(whole_answer.missing, whole_answer.gap)
            run.messages.append({"role": "user", "content": GAP_NOTE.format(gap=whole_answer.gap)})
            before = run.turn
            async for event in self._research(run, ctx, run.turn + allowed):
                yield event
            async for event in self._answer(run, ctx, question, conversation):
                yield event
            gap_left -= run.turn - before
            if run.answered is answered:  # no new answer (its research cut short): keep this one
                return

    async def _verify(
        self, run: _Run, ctx: ToolContext, question: str, layers: tuple[str, ...], rounds: int
    ) -> Verification:
        """The answer's checks: its citations' lines exist (one that doesn't is dropped), its
        names exist, its claims are supported, it is whole."""
        exists, files = existing(ctx, [c.cited for c in run.citations])
        kept = tuple(c for c, ok in zip(run.citations, exists, strict=True) if ok)
        gone = [c for c, ok in zip(run.citations, exists, strict=True) if not ok]
        run.dropped += len(gone)
        run.citations = kept
        found = claims(run.text, [(c.start, c.end, c.cited) for c in kept])
        flagged = tuple(
            claim.n
            for claim in claims(run.text, [(c.start, c.end, c.cited) for c in gone])
            if claim.cited
        )
        names = identifiers(run.text)
        strange = tuple(await unknown(ctx, names, run.shown)) if names else ()
        verdicts = await support(self.client, found, files, on_usage=self._counted(run))
        evidence = [b["title"] for b in run.evidence_blocks if b["type"] == "search_result"]
        answered = await whole(
            self.client,
            question,
            run.text,
            layers,
            evidence,
            deep=run.plan.escalated,
            on_usage=self._counted(run),
        )
        checked = tuple(Checked(claim, verdicts.get(claim.n)) for claim in found)
        return Verification(checked, len(gone), flagged, strange, answered, rounds, run.agreement)

    async def _research(self, run: _Run, ctx: ToolContext, turns: int) -> AsyncIterator[AgentEvent]:
        """The turns with tools, up to the ``turns``th of the run, until the agent commits to
        its evidence (or answers without, reminded once), or its turns or budget run out."""
        reminded = False
        run.stopped = "answered"
        while run.turn < turns:
            if run.spent.over:
                run.stopped = "budget"
                return
            run.turn += 1
            async for event in self._step(run, run.messages, tools=True):
                yield event
            result = run.result
            if result is None:
                raise RuntimeError(NO_RESULT)
            run.messages.append({"role": "assistant", "content": list(result.content)})
            if result.stop_reason == "tool_use":
                answered: list[dict[str, Any]] = []
                async for event in _call_tools(ctx, result, run, answered):
                    yield event
                run.messages.append({"role": "user", "content": answered})
                if ctx.evidence and _submitted(result):
                    return
                continue
            if result.stop_reason in CUT_SHORT or ctx.evidence or reminded:
                return
            reminded = True
            run.messages.append({"role": "user", "content": EVIDENCE_REMINDER})
        if turns > 0:
            run.stopped = "turns"

    async def _answer(
        self, run: _Run, ctx: ToolContext, question: str, conversation: Sequence[Exchange]
    ) -> AsyncIterator[AgentEvent]:
        """The answer turn: the question, after only the final evidence (4.4: what the agent
        committed to, then the best of the rest of what it read), no tools. None when the
        research was refused or cut off, or answered with nothing read to cite (its text is
        the answer)."""
        last = run.result
        if last is not None and last.stop_reason in CUT_SHORT:
            if run.answered is None:  # the run's only answer: its research's, as it is
                run.answered, run.index = last, run.read
            return
        pieces = gather(ctx.evidence, run.read.sources)
        if not pieces and last is not None and last.stop_reason != "tool_use":
            run.answered, run.index = last, run.read  # nothing read to cite: its own words
            return
        final = await final_set(
            ctx,
            question,
            pieces,
            self.reranker,
            on_rerank_usage=self.on_rerank_usage,
            on_llm_usage=self._counted(run),
        )
        blocks, sources, shown = answer_blocks(ctx, final)
        run.index = CitationIndex()
        run.index.add(sources)
        run.evidence_blocks = blocks
        run.shown = "\n".join(_shown_text(block) for block in blocks)
        yield EvidenceChosen(len(shown), sum(piece.submitted for piece in shown))
        note = STOPPED_NOTES.get(run.stopped)
        asked = question if note is None else f"{question}\n\n{note}"
        messages = [
            *_history(conversation, question)[:-1],
            {"role": "user", "content": [*blocks, {"type": "text", "text": asked}]},
        ]
        run.turn += 1
        async for event in self._step(run, messages, tools=False):
            yield event
        run.answered = run.result

    async def _step(
        self, run: _Run, messages: Sequence[dict[str, Any]], *, tools: bool
    ) -> AsyncIterator[AgentEvent]:
        """One turn on ``messages``: its start, its text as it streams; its result kept."""
        yield StepStarted(run.turn, run.model, run.plan.effort, tools=tools)
        run.result = None
        request = LLMTurn(
            tuple(messages),
            run.system,
            run.tools,
            run.plan.tier,
            effort=run.plan.effort,
            use_tools=tools,
        )
        async for event in self._turn(request, run.turn):
            if isinstance(event, LLMTurnResult):
                run.result = event
            else:
                yield event
        if run.result is None:
            raise RuntimeError(NO_RESULT)
        run.spent.add(run.result.usage)

    async def _turn(self, request: LLMTurn, turn: int) -> AsyncIterator[TextDelta | LLMTurnResult]:
        """One turn, its text yielded as it streams, then its result."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        running = asyncio.create_task(
            self.client.turn(request, on_text=queue.put, on_usage=self.on_usage)
        )
        try:
            while not running.done() or not queue.empty():
                waiting = asyncio.create_task(queue.get())
                await asyncio.wait({running, waiting}, return_when=asyncio.FIRST_COMPLETED)
                if waiting.done():
                    yield TextDelta(turn, waiting.result())
                else:
                    waiting.cancel()
        finally:
            if not running.done():
                running.cancel()
        yield await running


async def _round0(ctx: ToolContext, question: str, run: _Run) -> PreRetrieved | None:
    """Round 0's evidence into the question's message, before the question; its search results
    the request's first, so the index counts from them."""
    found = await pre_retrieve(ctx, question)
    if found is None:
        return None
    run.read.add(found.sources)
    content = [*found.content, {"type": "text", "text": question}]
    run.messages[-1] = {"role": "user", "content": content}
    run.agreement, run.trail = found.agreement, found.trail
    run.searched += [f"round 0 {text[:80]!r}" for text in found.searched]
    return PreRetrieved(found.results, found.files, found.traced, found.ms, found.agreement)


def _searched(name: str, args: dict[str, Any]) -> str:
    """A call as the run's searches list it: ``grep "items/{id}"``."""
    said = next((str(v) for v in args.values() if isinstance(v, str) and v), "")
    return f"{name} {said[:80]!r}" if said else name


def _shown_text(block: dict[str, Any]) -> str:
    if block["type"] == "text":
        return block["text"]
    return "\n".join([block["title"], *(part["text"] for part in block["content"])])


def _submitted(result: LLMTurnResult) -> bool:
    return any(
        block["type"] == "tool_use" and block["name"] == "submit_evidence"
        for block in result.content
    )


def _history(conversation: Sequence[Exchange], question: str) -> list[dict[str, Any]]:
    """The messages a run starts from: the earlier exchanges, then the question."""
    messages: list[dict[str, Any]] = []
    for earlier in conversation:
        messages += [
            {"role": "user", "content": earlier.question},
            {"role": "assistant", "content": earlier.answer},
        ]
    messages.append({"role": "user", "content": question})
    return messages


async def _call_tools(
    ctx: ToolContext,
    result: LLMTurnResult,
    run: _Run,
    answered: list[dict[str, Any]],
) -> AsyncIterator[ToolCalled | ToolAnswered]:
    """The turn's tool calls, run in order: each one's result into ``answered`` (one message
    for all of them), its search results into the run's citation index, the call into what the
    run searched, a trace's best path into its trail."""
    for call in (block for block in result.content if block["type"] == "tool_use"):
        yield ToolCalled(call["id"], call["name"], dict(call["input"]))
        started = time.monotonic()
        done = await run_tool(ctx, call["name"], call["input"])
        run.read.add(done.sources)
        if call["name"] != "submit_evidence":
            run.searched.append(_searched(call["name"], call["input"]))
        run.trail = done.trail or run.trail
        ms = round((time.monotonic() - started) * 1000)
        yield ToolAnswered(
            call["id"],
            call["name"],
            done.summary,
            done.hits,
            done.is_error,
            ms,
            tuple(done.content),
            done.card,
        )
        answered.append(done.param(call["id"]))


def _answer(
    content: Sequence[dict[str, Any]], index: CitationIndex
) -> tuple[str, tuple[AnswerCitation, ...], int]:
    """The answer's text, its citations (each a span of the text, and the lines it cites), and
    how many citations pointed at nothing the run gave."""
    text = ""
    citations: list[AnswerCitation] = []
    dropped = 0
    for block in content:
        if block.get("type") != "text":
            continue
        start = len(text)
        text += block["text"]
        for citation in block.get("citations") or ():
            if citation.get("type") != "search_result_location":
                dropped += 1
                continue
            cited = index.resolve(
                citation["search_result_index"],
                citation["start_block_index"],
                citation["end_block_index"],
            )
            if cited is None:
                dropped += 1
                continue
            citations.append(
                AnswerCitation(start, len(text), cited, citation.get("cited_text", ""))
            )
    return text, tuple(citations), dropped
