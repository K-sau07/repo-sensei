"""What a fix's agents (the investigation, the test writer) share around their tool calls: a
call that raises is the model's error result, never the stage's failure; each round of calls
ends the tools' transaction; each turn is priced (a model without a price is said, not free)."""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from decimal import Decimal
from typing import Any

from sensei_core.logging import get_logger
from sensei_core.usage import CostCalculator
from sensei_engine.agent.tools import ToolContext
from sensei_engine.agent.tools.base import ToolResult
from sensei_engine.llm.anthropic_client import PROVIDER
from sensei_fixer.stages import Cancelled

log = get_logger(__name__)


def text_result(text: str, summary: str, *, error: bool = False) -> ToolResult:
    return ToolResult([{"type": "text", "text": text}], [], summary, is_error=error)


# What stops a stage, never a tool call's error for the model: the worker's time limit (Celery's
# soft limit, by name: the fixer doesn't depend on Celery).
_STOPS_THE_STAGE = frozenset({"SoftTimeLimitExceeded", "TimeLimitExceeded"})


async def guarded(name: str, call: Awaitable[ToolResult]) -> ToolResult:
    """``call``'s result; if it raises (arguments no schema stopped, a bug), an error result
    saying so, so the model can go on. A cancel (or the budget) and the worker's time limit
    stop the stage instead."""
    try:
        return await call
    except Cancelled:
        raise
    except Exception as error:
        if type(error).__name__ in _STOPS_THE_STAGE:
            raise
        log.warning("fix tool call failed", tool=name, exc_info=True)
        said = f"{name} failed ({type(error).__name__}); check its arguments or try another way."
        return text_result(said, f"{name} failed", error=True)


async def settle(ctx: ToolContext) -> None:
    """End the tools' transaction (they only read): none stays open while the model thinks,
    and a query that failed doesn't fail the next round's."""
    db: Any = ctx.db
    if db is not None and db.in_transaction():
        await db.rollback()


def turn_cost(costs: CostCalculator | None, usage: Sequence[Any]) -> Decimal:
    """What a turn's usage cost (a model without a price counts nothing, and is warned of: the
    budget's cost cap can't see it)."""
    total = Decimal(0)
    if costs is None:
        return total
    for item in usage:
        priced = costs.cost(
            PROVIDER, item.model, tokens_in=item.input_tokens, tokens_out=item.output_tokens,
            tokens_cache_read=item.cache_read_tokens, tokens_cache_write=item.cache_write_tokens,
        )  # fmt: skip
        if priced is None:
            log.warning("no price for a fix agent's model", model=item.model)
        total += priced or Decimal(0)
    return total
