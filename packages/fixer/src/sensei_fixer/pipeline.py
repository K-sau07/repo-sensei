"""Running a fix's stages (8.4): one stage per call, the next queued for a worker.

:meth:`FixPipeline.run_stage`:

1. takes the stage (the run's row locked: only a run ``queued`` at the stage asked for moves
   on, so a duplicate or stale message, or a cancelled run, does nothing), and its agent run is
   made on the first stage;
2. runs the stage's handler with the row unlocked (stages are long: model turns, the sandbox),
   recording its steps and spend as it goes;
3. locks the row again and applies what the handler said, unless the run was cancelled
   meanwhile (then its result is dropped), and stops a run over its budget.

It answers with the stage to queue next (None: waiting for approval, done, or stopped). Events
are published after each commit. A handler that raises fails the run, saying which stage.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.fixes.tickets import Ticket
from sensei_core.logging import get_logger
from sensei_core.models import AgentRun, AgentStep, FixKind, FixRun, FixStage, FixStatus
from sensei_core.models.fixes import TERMINAL
from sensei_fixer.budget import Budget
from sensei_fixer.events import FixOutbox
from sensei_fixer.stages import Advance, Cancelled, Outcome, StageContext, StageHandler, Step, Stop
from sensei_fixer.state import FixStateMachine

log = get_logger(__name__)
Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]
# What a stage may set on the run (anything else in its updates is a bug, and refused).
FACTS = (
    "commit_sha", "root_cause", "repro", "plan", "repro_result", "patch", "verification", "risk",
    "merge_readiness", "diff_ref", "pr_url",
)  # fmt: skip
SETTABLE = frozenset(FACTS)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


@dataclass
class FixPipeline:
    sessions: Sessions
    redis: Redis
    handlers: Mapping[FixStage, StageHandler]
    budget: Budget
    clock: Callable[[], datetime] = utcnow

    async def _locked(
        self, db: AsyncSession, org_id: uuid.UUID, fix_id: uuid.UUID
    ) -> FixRun | None:
        await set_org_context(db, org_id)
        return await db.scalar(select(FixRun).where(FixRun.id == fix_id).with_for_update())

    async def run_stage(
        self, org_id: uuid.UUID, fix_id: uuid.UUID, stage: FixStage | None = None
    ) -> FixStage | None:
        """Run the stage the run is queued at (only if it is ``stage``, when given)."""
        taken = await self._take(org_id, fix_id, stage)
        if taken is None:
            return None
        ctx, outbox = taken
        handler = self.handlers.get(ctx.stage)
        outcome: Outcome | None
        try:
            outcome = (
                await handler(ctx) if handler is not None
                else Stop(FixStatus.FAILED, f"No handler for the {ctx.stage.value} stage.")
            )  # fmt: skip
        except Cancelled:
            outcome = None  # cancelled (dropped below) or over budget (stopped below)
        except Exception as error:
            log.exception("fix stage failed", fix_id=str(fix_id), stage=ctx.stage.value)
            if type(error).__name__ == "SoftTimeLimitExceeded":  # the worker's time limit
                said = f"The {ctx.stage.value} stage ran out of time."
            else:
                said = f"The {ctx.stage.value} stage failed ({type(error).__name__})."
            outcome = Stop(FixStatus.FAILED, said)
        return await self._finish(org_id, fix_id, ctx.stage, outcome, outbox)

    async def _take(
        self, org_id: uuid.UUID, fix_id: uuid.UUID, wanted: FixStage | None
    ) -> tuple[StageContext, FixOutbox] | None:
        async with self.sessions() as db:
            run = await self._locked(db, org_id, fix_id)
            if run is None or run.status is not FixStatus.QUEUED:
                return None
            if wanted is not None and run.stage is not wanted:
                return None  # a message for a stage it has left (it is on another queue)
            machine = FixStateMachine(run, self.clock)
            stage = machine.begin_stage()
            if run.run_id is None:
                kind = (run.kind or FixKind.FIX).value
                agent = AgentRun(
                    org_id=run.org_id, repo_id=run.repo_id, user_id=run.created_by, kind=kind,
                    mode=kind, question=run.title,
                )  # fmt: skip
                db.add(agent)
                await db.flush()
                run.run_id = agent.id
            outbox = FixOutbox(run.org_id, run.id, run.repo_id)
            outbox.add(machine.events)
            agent_id = run.run_id
            facts = {name: _value(getattr(run, name)) for name in FACTS}
            ticket = Ticket.model_validate(run.ticket)
            repo_id = run.repo_id
            kind = run.kind or FixKind.FIX
        await outbox.publish(self.redis)
        ctx = StageContext(
            fix_id=fix_id, org_id=org_id, repo_id=repo_id, stage=stage, ticket=ticket,
            facts=facts, record=self._recorder(org_id, fix_id, agent_id, repo_id),
            spend=self._spender(org_id, fix_id, agent_id), check=self._checker(org_id, fix_id),
            kind=kind,
        )  # fmt: skip
        return ctx, outbox

    def _recorder(
        self, org_id: uuid.UUID, fix_id: uuid.UUID, agent_id: uuid.UUID, repo_id: uuid.UUID
    ) -> Callable[[Step], Any]:
        async def record(step: Step) -> None:
            async with self.sessions() as db:
                await set_org_context(db, org_id)
                idx = await db.scalar(
                    select(func.coalesce(func.max(AgentStep.idx) + 1, 0)).where(
                        AgentStep.run_id == agent_id
                    )
                )
                db.add(
                    AgentStep(
                        org_id=org_id, run_id=agent_id, idx=idx or 0, type=step.type,
                        name=step.name[:64], input=dict(step.input), output=step.output,
                        summary=step.summary, tokens=step.tokens, ms=step.ms,
                        started_at=self.clock(),
                    )
                )  # fmt: skip
            outbox = FixOutbox(org_id, fix_id, repo_id)
            outbox.add([{"type": "step", "fix_id": str(fix_id), "name": step.name,
                         "summary": step.summary, "ts": self.clock().isoformat()}])  # fmt: skip
            await outbox.publish(self.redis)

        return record

    def _spender(
        self, org_id: uuid.UUID, fix_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Callable[[int, Decimal, float], Any]:
        async def spend(turns: int, cost: Decimal, sandbox_seconds: float) -> None:
            async with self.sessions() as db:
                await set_org_context(db, org_id)
                await db.execute(
                    update(FixRun)
                    .where(FixRun.id == fix_id)
                    .values(
                        spent_turns=FixRun.spent_turns + turns,
                        spent_cost_usd=FixRun.spent_cost_usd + cost,
                        spent_sandbox_seconds=FixRun.spent_sandbox_seconds + sandbox_seconds,
                    )
                )
                await db.execute(
                    update(AgentRun)
                    .where(AgentRun.id == agent_id)
                    .values(turns=AgentRun.turns + turns, cost_usd=AgentRun.cost_usd + cost)
                )

        return spend

    def _checker(self, org_id: uuid.UUID, fix_id: uuid.UUID) -> Callable[[], Any]:
        async def check() -> None:
            async with self.sessions() as db:
                await set_org_context(db, org_id)
                run = await db.get(FixRun, fix_id)
                if run is None or run.status is not FixStatus.RUNNING:
                    raise Cancelled("cancelled")
                if self.budget.exhausted(run) is not None:
                    raise Cancelled("budget used up")

        return check

    async def _finish(
        self,
        org_id: uuid.UUID,
        fix_id: uuid.UUID,
        stage: FixStage,
        outcome: Outcome | None,
        outbox: FixOutbox,
    ) -> FixStage | None:
        started = time.monotonic()
        async with self.sessions() as db:
            run = await self._locked(db, org_id, fix_id)
            if run is None or run.status is not FixStatus.RUNNING or run.stage is not stage:
                return None  # cancelled while it worked: what it found is dropped
            machine = FixStateMachine(run, self.clock)
            over = self.budget.over(run)
            if over is not None:
                outcome = Stop(FixStatus.FAILED, f"Stopped: {over}.")
            elif outcome is None:  # it was stopped (its budget), yet the run goes on: stop it
                used = self.budget.exhausted(run)
                said = f"Stopped: {used}." if used else f"The {stage.value} stage stopped."
                outcome = Stop(FixStatus.FAILED, said)
            unknown = set(outcome.updates) - SETTABLE
            if unknown:
                said = f"The {stage.value} stage set {', '.join(sorted(unknown))}."
                outcome = Stop(FixStatus.FAILED, said)
            for name, value in outcome.updates.items():
                setattr(run, name, value)
            if isinstance(outcome, Advance):
                machine.advance()
            else:
                machine.stop(outcome.status, outcome.reason)
            await close_agent_run(db, run, self.clock)
            outbox.add(machine.events)
            following = queued_stage(run)
            status = run.status.value
        await outbox.publish(self.redis)
        ms = round((time.monotonic() - started) * 1000)
        log.info("fix stage finished", fix_id=str(fix_id), stage=stage.value, status=status, ms=ms)
        return following


async def close_agent_run(
    db: AsyncSession, run: FixRun, clock: Callable[[], datetime] = utcnow
) -> None:
    """Close ``run``'s agent run once the fix has ended (however: a stage, a cancel, the sweep)."""
    if run.run_id is None or run.status not in TERMINAL:
        return
    agent = await db.get(AgentRun, run.run_id)
    if agent is None or agent.finished_at is not None:
        return
    agent.status = "succeeded" if run.status is FixStatus.SUCCEEDED else "failed"
    agent.stopped = None if run.status is FixStatus.SUCCEEDED else run.status.value[:16]
    agent.finished_at = clock()


def queued_stage(run: FixRun) -> FixStage | None:
    """The stage ``run`` waits for a worker at (None: it isn't waiting for one)."""
    return run.stage if run.status is FixStatus.QUEUED else None


def cancel(run: FixRun, *, by: str, clock: Callable[[], datetime] = utcnow) -> list[dict[str, Any]]:
    """Cancel ``run`` (the caller holds it locked); its events. A stage still working finds out
    at its next check, and its result is dropped."""
    machine = FixStateMachine(run, clock)
    machine.stop(FixStatus.CANCELLED, f"Cancelled by {by}.")
    return machine.events
