"""The loop the planner, the patch agent and the verifier (8.7) share: a model's turns, each
priced against the fix's budget and recorded as a step, its tool calls answered (a call that
raises is the model's error result), the tools' transaction ended after each round, and the run
checked for a cancel (or its budget) before each turn.

What a call does is the agent's own (``answer``); the conversation ends when the agent says it
is done (``finished``), or with why not: the model declined, stopped calling tools after a
reminder, or ran out of turns.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from sensei_core.usage import CostCalculator
from sensei_engine.agent.tools import ToolContext
from sensei_engine.agent.tools.base import ToolResult
from sensei_engine.llm.anthropic_client import AnthropicClient, Effort, LLMTurn, Tier
from sensei_fixer.investigate import Hooks
from sensei_fixer.stages import Step
from sensei_fixer.tooling import guarded, settle, turn_cost

Answer = Callable[[str, Mapping[str, Any]], Awaitable[ToolResult]]


@dataclass
class Conversation:
    client: AnthropicClient
    costs: CostCalculator | None
    hooks: Hooks
    tools_ctx: ToolContext
    system: str
    tools: list[dict[str, Any]]
    tier: Tier
    effort: Effort
    max_turns: int
    reminder: str  # said once when the model answers without calling a tool
    what: str  # what it ends without ("a plan"), for the reasons it gives

    async def run(self, first: str, answer: Answer, finished: Callable[[], bool]) -> str | None:
        """Talk from ``first`` until ``finished()`` (None), or why it ended without."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": first}]
        reminded = False
        for turn in range(1, self.max_turns + 1):
            await self.hooks.check()
            started = time.monotonic()
            request = LLMTurn(tuple(messages), (self.system,), self.tools, self.tier,
                              effort=self.effort)  # fmt: skip
            result = await self.client.turn(request)
            tokens = sum(u.input_tokens + u.output_tokens for u in result.usage)
            await self.hooks.spend(1, turn_cost(self.costs, result.usage), 0.0)
            calls = [b for b in result.content if b.get("type") == "tool_use"]
            await self.hooks.record(Step(
                "llm", f"turn {turn}", ", ".join(c["name"] for c in calls) or "no tools",
                tokens=tokens, ms=round((time.monotonic() - started) * 1000),
            ))  # fmt: skip
            messages.append({"role": "assistant", "content": list(result.content)})
            if result.stop_reason == "refusal":
                return "The model declined."
            if not calls:
                if reminded:
                    return f"It stopped without {self.what}."
                reminded = True
                messages.append({"role": "user", "content": self.reminder})
                continue
            answers: list[dict[str, Any]] = []
            notes: list[dict[str, Any]] = []
            for call in calls:
                name = str(call["name"])
                args = cast("Mapping[str, Any]", call.get("input") or {})
                began = time.monotonic()
                done = await guarded(name, answer(name, args))
                ms = round((time.monotonic() - began) * 1000)
                await self.hooks.record(Step("tool", name, done.summary, input=_shown(args), ms=ms))
                answers.append(done.param(str(call["id"])))
                notes += done.notes(name)
            await settle(self.tools_ctx)
            if finished():
                return None
            messages.append({"role": "user", "content": [*answers, *notes]})
        return f"No {self.what} in {self.max_turns} turns."


def _shown(args: Mapping[str, Any]) -> dict[str, Any]:
    """A call's arguments as a step keeps them (long texts, a file's content, cut short)."""
    return {k: (v[:2000] + "...") if isinstance(v, str) and len(v) > 2000 else v
            for k, v in args.items()}  # fmt: skip
