"""An agent run's record (4.8), written as it runs: its steps in order, what each took, and the
run's totals when it ends.

The :class:`Recorder` follows the agent's events and its usage reports:

* **a model turn** (``llm``) runs from its start until its usage arrives (the turn's end);
* **a tool call** (``tool``): its input (redacted), its result's summary and what the model
  was given (in the row when small, else in object storage, the row keeping its key);
* **retrieval** (round 0, the final evidence) and **checks** (``verify``), each with the
  tokens the models spent for it (usage that arrives outside a turn goes to the next of
  these).

Each step is stored as it ends, and published to the run's Redis stream (``run:<id>``) for a
live timeline. Everything stored passes the redaction processor.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import orjson
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import AgentRun, AgentStep
from sensei_core.redaction import redact
from sensei_core.storage import Bucket, repo_prefix
from sensei_core.usage import CostCalculator
from sensei_engine.agent.loop import (
    AgentEvent,
    Answer,
    CitationFound,
    Done,
    EvidenceChosen,
    GapFound,
    PreRetrieved,
    StepStarted,
    TextDelta,
    ToolAnswered,
    ToolCalled,
    Verified,
)
from sensei_engine.llm.anthropic_client import PROVIDER, LLMUsage

RUN_PREFIX = "run:"
RUN_STREAM_SECONDS = 3_600
Put = Callable[[str, bytes], Awaitable[None]]


class Recorder:
    def __init__(
        self,
        db: AsyncSession,
        run: AgentRun,
        *,
        redis: Redis | None,
        costs: CostCalculator,
        put: Put | None,
        inline_bytes: int,
    ) -> None:
        self.db = db
        self.run = run
        self.redis = redis
        self.costs = costs
        self.put = put
        self.inline_bytes = inline_bytes
        self.started = time.monotonic()
        for counter in ("turns", "tokens_in", "tokens_out", "cache_read", "cache_write"):
            if getattr(run, counter) is None:  # a new row's, until the database fills it in
                setattr(run, counter, 0)
        self._idx = 0
        self._llm: AgentStep | None = None  # the model turn under way
        self._llm_started = 0.0
        self._answering = False  # the open turn writes the answer (it may call no tools)
        self._tools: dict[str, AgentStep] = {}
        self._pending = 0  # tokens spent outside a turn, for the next retrieval or check
        self._cost = Decimal(0)

    # ------------------------------------------------------------ what the agent says

    async def usage(self, usage: LLMUsage) -> None:
        tokens = usage.input_tokens + usage.output_tokens
        tokens += usage.cache_read_tokens + usage.cache_write_tokens
        run = self.run
        run.tokens_in += usage.input_tokens
        run.tokens_out += usage.output_tokens
        run.cache_read += usage.cache_read_tokens
        run.cache_write += usage.cache_write_tokens
        priced = self.costs.cost(
            PROVIDER,
            usage.model,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            tokens_cache_read=usage.cache_read_tokens,
            tokens_cache_write=usage.cache_write_tokens,
            batch=usage.batch,
        )
        self._cost += priced or Decimal(0)
        if self._llm is not None:  # the turn's own usage: its end
            self._llm.tokens += tokens
            self._llm.ms = self._ms_since(self._llm_started)
            await self._store(self._llm)
            self._llm = None
        else:
            self._pending += tokens

    async def event(self, event: AgentEvent) -> None:
        match event:
            case StepStarted():
                self.run.turns = event.turn
                self.run.model = event.model
                self._llm = self._step(
                    "llm",
                    event.model,
                    {"turn": event.turn, "effort": event.effort, "tools": event.tools},
                )
                self._llm_started = time.monotonic()
                self._answering = not event.tools
            case TextDelta():
                # The answer's first token: a turn that may call tools writes reasoning, not it.
                if self.run.first_token_ms is None and self._answering and event.text:
                    self.run.first_token_ms = self._ms_since(self.started)
            case ToolCalled():
                self._tools[event.id] = self._step("tool", event.name, event.input)
            case ToolAnswered():
                await self._answered(event)
            case PreRetrieved():
                summary = f"{event.results} results in {event.files} files"
                step = self._step("retrieval", "round0", {"traced": event.traced})
                await self._finish(step, summary, ms=event.ms)
            case EvidenceChosen():
                step = self._step("retrieval", "evidence", {})
                summary = f"{event.pieces} pieces, {event.submitted} submitted"
                await self._finish(step, summary)
            case Verified(verification=check):
                step = self._step("verify", "verify", {"rounds": check.rounds})
                summary = f"{check.supported} of {check.cited} cited claims supported"
                await self._finish(step, summary)
            case GapFound():
                step = self._step("verify", "gap", {"missing": list(event.missing)})
                await self._finish(step, event.gap)
            case Done():  # its message keeps the answer
                if self.run.first_token_ms is None and event.answer.text:
                    # A research turn's words, sent as the answer once the run is done.
                    self.run.first_token_ms = self._ms_since(self.started)
            case CitationFound():  # the answer's: its message keeps them
                return

    async def finish(self, answer: Answer | None, error: str | None = None) -> None:
        """The run's totals, once it has ended (with its answer, or what went wrong)."""
        run = self.run
        if self._llm is not None:  # a turn cut off by the failure
            self._llm.ms = self._ms_since(self._llm_started)
            await self._store(self._llm)
            self._llm = None
        run.status = "succeeded" if answer is not None and error is None else "failed"
        run.error = error
        run.cost_usd = self._cost
        run.latency_ms = self._ms_since(self.started)
        run.finished_at = datetime.now(UTC)
        if answer is not None:
            run.stopped = None if answer.stopped == "answered" else answer.stopped
            run.turns = answer.turns
        await self.db.flush()
        await self._publish({"type": "finished", "status": run.status})

    # ------------------------------------------------------------ steps

    def _step(self, kind: str, name: str, given: dict[str, Any]) -> AgentStep:
        step = AgentStep(
            org_id=self.run.org_id,
            run_id=self.run.id,
            idx=self._idx,
            type=kind,
            name=name[:64],
            input=redact(given),
            summary="",
            tokens=0,
            ms=0,
            started_at=datetime.now(UTC),
        )
        self._idx += 1
        return step

    async def _answered(self, event: ToolAnswered) -> None:
        step = self._tools.pop(event.id, None)
        if step is None:
            return
        output = redact(list(event.content))
        raw = orjson.dumps(output)
        if len(raw) <= self.inline_bytes or self.put is None:
            step.output = output if len(raw) <= self.inline_bytes else None
        else:
            # Under the repository's prefix: purged with it.
            prefix = repo_prefix(Bucket.ARTIFACTS, self.run.org_id, self.run.repo_id)
            key = f"{prefix}runs/{self.run.id}/{step.idx}.json"
            await self.put(key, raw)
            step.output_ref = key
        suffix = " (failed)" if event.is_error else ""
        await self._finish(step, f"{event.summary}{suffix}", ms=event.ms)

    async def _finish(self, step: AgentStep, summary: str, *, ms: int | None = None) -> None:
        step.summary = summary
        step.ms = ms if ms is not None else 0
        step.tokens += self._pending
        self._pending = 0
        await self._store(step)

    async def _store(self, step: AgentStep) -> None:
        self.db.add(step)
        await self.db.flush()
        await self._publish(step_data(step, self.run))

    async def _publish(self, data: dict[str, Any]) -> None:
        if self.redis is None:
            return
        key = f"{RUN_PREFIX}{self.run.id}"
        await self.redis.xadd(key, {"step": orjson.dumps(data)})  # pyright: ignore[reportUnknownMemberType]
        await self.redis.expire(key, RUN_STREAM_SECONDS)

    def _ms_since(self, moment: float) -> int:
        return round((time.monotonic() - moment) * 1000)


def step_data(step: AgentStep, run: AgentRun) -> dict[str, Any]:
    """A step as the timeline reads it: when it started into the run, what it took."""
    offset = round((step.started_at - run.created_at).total_seconds() * 1000)
    return {
        "type": "step",
        "idx": step.idx,
        "kind": step.type,
        "name": step.name,
        "input": step.input,
        "summary": step.summary,
        "tokens": step.tokens,
        "ms": step.ms,
        "offset_ms": max(0, offset),
        "has_output": step.output is not None or step.output_ref is not None,
    }
