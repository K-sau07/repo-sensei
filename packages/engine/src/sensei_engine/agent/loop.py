"""The Ask agent's loop (4.2): a question answered with the tools, in a few bounded turns.

* **Model and effort**, by what the question is (the query router's rules): Sonnet at effort
  ``low`` for a lookup (a name, a string), ``medium`` for the rest; **Opus** at ``high`` for a
  question that cuts across the repository (its architecture, a change's impact), in deep mode,
  or when the caller escalates (a verifier that found the answer short, 4.5).
* **At most** :attr:`Budget.max_turns` **turns**. The model calls tools as it likes
  (``tool_choice`` auto; it is told to call independent ones together), and every result of a
  turn goes back in one message. The last turn, or the first past the budget's tokens or
  dollars, is an answer: no tools, and a note of why.
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
from sensei_engine.agent.overview import repo_overview
from sensei_engine.agent.tools import CitationIndex, ToolContext, definitions, run_tool
from sensei_engine.agent.tools.base import Cited, Evidence
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
from sensei_engine.search.route import QueryClass, Route, classify

PROMPT_VERSION = "ask_system.v1"
LOOKUPS = frozenset({QueryClass.SYMBOL, QueryClass.REGEX})
CROSS_CUTTING = frozenset({QueryClass.ARCHITECTURE, QueryClass.IMPACT})
LAST_TURN_NOTE = (
    "That was your last turn of tool calls. Answer now from what you have read, and say what "
    "you couldn't check."
)
BUDGET_NOTE = (
    "This question has used its budget. Answer now from what you have read, and say what you "
    "couldn't check."
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
    dropped_citations: int = 0  # citations that point at nothing the run gave


@dataclass(frozen=True, slots=True)
class Done:
    answer: Answer


AgentEvent = StepStarted | ToolCalled | ToolAnswered | TextDelta | CitationFound | Done


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


class AskAgent:
    def __init__(
        self,
        client: AnthropicClient,
        *,
        budget: Budget | None = None,
        costs: CostCalculator | None = None,
        on_usage: LLMUsageSink | None = None,
    ) -> None:
        self.client = client
        self.budget = budget or Budget()
        self.costs = costs
        self.on_usage = on_usage

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
        plan = plan_for(classify(question).route, mode, escalate=escalate)
        model = self.client.model_for(plan.tier)
        system = (system_prompt(), overview or await repo_overview(ctx))
        tools = definitions()
        messages = _history(conversation, question)
        index = CitationIndex()
        spent = _Spent(self.budget, self.costs)
        stopped: Stop = "answered"
        result: LLMTurnResult | None = None
        turn = 0
        while turn < self.budget.max_turns:
            turn += 1
            last = turn == self.budget.max_turns or spent.over
            if last and turn > 1:
                stopped = "budget" if spent.over else "turns"
                note = BUDGET_NOTE if stopped == "budget" else LAST_TURN_NOTE
                messages[-1]["content"].append({"type": "text", "text": note})
            yield StepStarted(turn, model, plan.effort, tools=not last)
            result = None
            request = LLMTurn(
                tuple(messages), system, tools, plan.tier, effort=plan.effort, use_tools=not last
            )
            async for event in self._turn(request, turn):
                if isinstance(event, LLMTurnResult):
                    result = event
                else:
                    yield event
            if result is None:
                raise RuntimeError(NO_RESULT)
            spent.add(result.usage)
            messages.append({"role": "assistant", "content": list(result.content)})
            if result.stop_reason != "tool_use" or last:
                break
            answered: list[dict[str, Any]] = []
            async for event in _call_tools(ctx, result, index, answered):
                yield event
            messages.append({"role": "user", "content": answered})
        if result is None:
            raise RuntimeError(NO_RESULT)
        if result.stop_reason == "max_tokens":
            stopped = "truncated"
        elif result.stop_reason == "refusal":
            stopped = "refused"
        text, citations, dropped = _answer(result.content, index)
        for n, citation in enumerate(citations, start=1):
            yield CitationFound(n, citation)
        yield Done(
            Answer(
                text,
                citations,
                stopped,
                result.model,
                turn,
                tuple(spent.usage),
                spent.cost if self.costs is not None else None,
                tuple(ctx.evidence),
                plan.escalated,
                dropped,
            )
        )

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
    index: CitationIndex,
    answered: list[dict[str, Any]],
) -> AsyncIterator[ToolCalled | ToolAnswered]:
    """The turn's tool calls, run in order: each one's result into ``answered`` (one message
    for all of them) and its search results into the run's citation index."""
    for call in (block for block in result.content if block["type"] == "tool_use"):
        yield ToolCalled(call["id"], call["name"], dict(call["input"]))
        started = time.monotonic()
        done = await run_tool(ctx, call["name"], call["input"])
        index.add(done.sources)
        ms = round((time.monotonic() - started) * 1000)
        yield ToolAnswered(call["id"], call["name"], done.summary, done.hits, done.is_error, ms)
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
