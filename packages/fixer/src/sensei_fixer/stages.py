"""What each stage does (8.4): a handler per stage, given the run's facts and a way to record
steps, spend budget and see whether it should stop; it answers with what to set on the run and
whether to go on (:class:`Advance`) or stop (:class:`Stop`).

The pipeline owns the moves; a handler never changes the run's status itself. The stages' real
work arrives step by step (investigation 8.5, reproduction 8.6, patch and verification 8.7, the
pull request 8.9); until a stage has its handler, a run stops there, saying so.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from sensei_core.fixes.tickets import Ticket
from sensei_core.models import FixKind, FixStage, FixStatus


class Cancelled(Exception):
    """The run was cancelled (or went over budget) while the stage worked."""


@dataclass(frozen=True, slots=True)
class Advance:
    """Done: these fields set on the run (``root_cause``, ``plan``, ``repro_result``...)."""

    updates: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class Stop:
    status: FixStatus  # not_reproduced, blocked, failed
    reason: str
    updates: Mapping[str, Any] = field(default_factory=dict[str, Any])


Outcome = Advance | Stop


@dataclass(frozen=True, slots=True)
class Step:
    type: str  # llm, tool, verify, retrieval (agent_steps' types)
    name: str
    summary: str = ""
    input: Mapping[str, Any] = field(default_factory=dict[str, Any])
    output: Any = None
    tokens: int = 0
    ms: int = 0


@dataclass(frozen=True, slots=True)
class StageContext:
    """What a handler gets. ``record`` saves a step (and streams it); ``spend`` counts against
    the budget; ``check`` raises :class:`Cancelled` if the run was cancelled or is over budget
    (call it between long steps)."""

    fix_id: uuid.UUID
    org_id: uuid.UUID
    repo_id: uuid.UUID
    stage: FixStage
    ticket: Ticket
    facts: Mapping[str, Any]  # the run's root_cause, plan, repro_result... so far
    record: Callable[[Step], Awaitable[None]]
    spend: Callable[[int, Decimal, float], Awaitable[None]]
    check: Callable[[], Awaitable[None]]
    kind: FixKind = FixKind.FIX  # a fix, or a requested change (9.1)


class StageHandler(Protocol):
    async def __call__(self, ctx: StageContext, /) -> Outcome: ...


def by_kind(handlers: Mapping[FixKind, StageHandler]) -> StageHandler:
    """One stage done one way for a fix and another for a change (the plan: a fix's from its
    root cause, a change's from its request); a kind with none stops, saying so."""

    async def handler(ctx: StageContext) -> Outcome:
        found = handlers.get(ctx.kind)
        if found is None:
            stage = ctx.stage.value.replace("_", " ")
            return Stop(FixStatus.FAILED, f"A {ctx.kind.value} has no {stage} stage.")
        return await found(ctx)

    return handler


async def intake(ctx: StageContext) -> Outcome:
    """The ticket is what intake made of the issue: something must be there to work from."""
    if not (ctx.ticket.text.strip() or ctx.ticket.frames or ctx.ticket.title.strip()):
        return Stop(FixStatus.FAILED, "The issue says nothing to start from.")
    culprit = ctx.ticket.culprit
    where = f" at {culprit.path}:{culprit.line}" if culprit else ""
    await ctx.record(Step("tool", "intake", f"{ctx.ticket.error_type or 'The issue'}{where}"))
    return Advance()


def not_built(stage: FixStage) -> StageHandler:
    async def handler(_ctx: StageContext) -> Outcome:
        return Stop(FixStatus.FAILED, f"The {stage.value.replace('_', ' ')} stage isn't built yet.")

    return handler


def default_handlers() -> dict[FixStage, StageHandler]:
    found: dict[FixStage, StageHandler] = {
        stage: not_built(stage)
        for stage in FixStage
        if stage not in (FixStage.AWAITING_APPROVAL, FixStage.DONE)
    }
    found[FixStage.INTAKE] = intake
    return found
