"""Running a fix's stages (8.4) against the database: one stage per call and the next one
named, steps and spend recorded, events streamed; a stop, a failing stage, a cancel while a
stage works (its result dropped), the budget, and a duplicate delivery."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import orjson
import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.fixes.tickets import parse_ticket
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AgentRun,
    AgentStep,
    FixRun,
    FixSource,
    FixStage,
    FixStatus,
    Repo,
    RepoSource,
)
from sensei_core.orgs.service import OrgService
from sensei_fixer.budget import Budget
from sensei_fixer.events import fix_stream
from sensei_fixer.pipeline import FixPipeline, cancel
from sensei_fixer.stages import (
    Advance,
    Outcome,
    StageContext,
    StageHandler,
    Step,
    Stop,
    default_handlers,
)
from sensei_fixer.state import FixStateMachine
from sensei_testing import make_user

pytestmark = pytest.mark.integration
TRACE = "Traceback (most recent call last):\n  File \"/a/b.py\", line 3, in f\nKeyError: 'x'\n"


async def _fix(db: AsyncSession) -> FixRun:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "http://localhost:3000").create(
        db, owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}"
    )
    repo = Repo(
        org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=7, owner="o",
        name="items", full_name="o/items", slug="items", default_branch="main",
        visibility="public",
    )  # fmt: skip
    db.add(repo)
    await db.flush()
    run = FixRun(
        org_id=repo.org_id, repo_id=repo.id, source=FixSource.PASTE, title="KeyError: 'x'",
        ticket=parse_ticket(TRACE).model_dump(mode="json"), status=FixStatus.QUEUED,
        stage=FixStage.INTAKE, created_by=owner.id,
    )  # fmt: skip
    db.add(run)
    await db.flush()
    return run


def _pipeline(
    db: AsyncSession, redis: FakeAsyncRedis, handlers: dict[FixStage, StageHandler],
    budget: Budget | None = None,
) -> FixPipeline:  # fmt: skip
    @asynccontextmanager
    async def same() -> AsyncGenerator[AsyncSession]:
        # The pipeline's own sessions are the test's (rolled back after): flushed where a real
        # one would commit.
        yield db
        await db.flush()

    return FixPipeline(sessions=same, redis=redis, handlers=handlers, budget=budget or Budget())


def _advancing(**updates: Any) -> StageHandler:
    async def handler(ctx: StageContext) -> Outcome:
        await ctx.record(Step("tool", ctx.stage.value, f"did {ctx.stage.value}"))
        await ctx.spend(1, Decimal("0.01"), 0.0)
        return Advance(updates)

    return handler


async def _events(redis: FakeAsyncRedis, fix_id: uuid.UUID) -> list[dict[str, Any]]:
    entries = await redis.xrange(fix_stream(fix_id))  # pyright: ignore[reportUnknownMemberType]
    return [orjson.loads(fields.get("data") or fields[b"data"]) for _id, fields in entries]


async def test_stages_run_one_at_a_time_to_approval_then_to_done(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)
    handlers = {stage: _advancing() for stage in FixStage}
    handlers[FixStage.INTAKE] = default_handlers()[FixStage.INTAKE]
    handlers[FixStage.ROOT_CAUSE] = _advancing(root_cause={"claim": "a missing key"})
    handlers[FixStage.PR] = _advancing(pr_url="https://github.com/o/items/pull/9")
    pipeline = _pipeline(db_session, fake_redis, handlers)
    reached: list[FixStage] = []
    nxt = await pipeline.run_stage(run.org_id, run.id)
    while nxt is not None:
        reached.append(nxt)
        nxt = await pipeline.run_stage(run.org_id, run.id)
    assert reached == [FixStage.INVESTIGATE, FixStage.REPRODUCE, FixStage.ROOT_CAUSE,
                       FixStage.PLAN]  # fmt: skip
    await db_session.refresh(run)
    assert (run.status, run.stage) == (FixStatus.AWAITING_APPROVAL, FixStage.AWAITING_APPROVAL)
    assert run.root_cause == {"claim": "a missing key"}
    FixStateMachine(run).approve()
    await db_session.flush()
    while (nxt := await pipeline.run_stage(run.org_id, run.id)) is not None:
        reached.append(nxt)
    await db_session.refresh(run)
    assert (run.status, run.pr_url) == (FixStatus.SUCCEEDED, "https://github.com/o/items/pull/9")
    assert (run.spent_turns, run.spent_cost_usd) == (7, Decimal("0.07"))
    agent = await db_session.get(AgentRun, run.run_id)
    assert agent is not None
    assert (agent.kind, agent.status, agent.turns) == ("fix", "succeeded", 7)
    steps = (await db_session.scalars(
        select(AgentStep).where(AgentStep.run_id == agent.id).order_by(AgentStep.idx)
    )).all()  # fmt: skip
    assert [s.idx for s in steps] == list(range(8))
    assert steps[0].name == "intake"
    kinds = [e["type"] for e in await _events(fake_redis, run.id)]
    assert kinds.count("stage_started") == 8
    assert "awaiting_approval" in kinds
    assert kinds[-1] == "done"


async def test_a_stage_that_stops_or_raises_ends_the_fix(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)

    async def not_reproduced(_ctx: StageContext) -> Outcome:
        return Stop(FixStatus.NOT_REPRODUCED, "The test passed 3 times of 3.",
                    {"repro_result": "not_reproduced"})  # fmt: skip

    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: not_reproduced})
    assert await pipeline.run_stage(run.org_id, run.id) is None
    await db_session.refresh(run)
    assert (run.status, run.stop_reason) == (FixStatus.NOT_REPRODUCED,
                                             "The test passed 3 times of 3.")  # fmt: skip
    assert run.repro_result is not None
    assert (await _events(fake_redis, run.id))[-1]["type"] == "not_reproduced"

    broken = await _fix(db_session)

    async def boom(_ctx: StageContext) -> Outcome:
        raise RuntimeError("the model went away")

    await _pipeline(db_session, fake_redis, {FixStage.INTAKE: boom}).run_stage(
        broken.org_id, broken.id
    )
    await db_session.refresh(broken)
    assert (broken.status, broken.stop_reason) == (
        FixStatus.FAILED, "The intake stage failed (RuntimeError).",
    )  # fmt: skip
    agent = await db_session.get(AgentRun, broken.run_id)
    assert agent is not None
    assert (agent.status, agent.stopped) == ("failed", "failed")


async def test_a_cancel_while_a_stage_works_drops_its_result(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)
    seen: list[str] = []

    async def cancelled_meanwhile(ctx: StageContext) -> Outcome:
        await set_org_context(db_session, ctx.org_id)
        found = await db_session.get(FixRun, ctx.fix_id)
        assert found is not None
        cancel(found, by="@ada")  # a person cancels from the page
        await db_session.flush()
        try:
            await ctx.check()
        except Exception as error:
            seen.append(type(error).__name__)
            raise
        return Advance({"root_cause": {"claim": "should be dropped"}})

    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: cancelled_meanwhile})
    assert await pipeline.run_stage(run.org_id, run.id) is None
    await db_session.refresh(run)
    assert seen == ["Cancelled"]
    assert (run.status, run.root_cause) == (FixStatus.CANCELLED, None)
    assert run.stop_reason == "Cancelled by @ada."


async def test_a_fix_over_its_budget_stops(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)

    async def costly(ctx: StageContext) -> Outcome:
        await ctx.spend(3, Decimal("2.50"), 120.0)
        return Advance()

    budget = Budget(max_turns=100, max_cost_usd=Decimal("2.00"), max_sandbox_seconds=600)
    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: costly}, budget)
    assert await pipeline.run_stage(run.org_id, run.id) is None
    await db_session.refresh(run)
    assert (run.status, run.stop_reason) == (FixStatus.FAILED, "Stopped: it cost more than $2.00.")


async def test_only_a_queued_fix_moves_and_stages_set_only_their_fields(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)
    run.status = FixStatus.AWAITING_APPROVAL
    await db_session.flush()
    calls: list[str] = []

    async def counted(_ctx: StageContext) -> Outcome:
        calls.append("ran")
        return Advance()

    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: counted})
    assert await pipeline.run_stage(run.org_id, run.id) is None  # a duplicate, or not its turn
    assert calls == []
    sneaky = await _fix(db_session)
    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: _advancing(status="done")})
    await pipeline.run_stage(sneaky.org_id, sneaky.id)
    await db_session.refresh(sneaky)
    assert (sneaky.status, sneaky.stop_reason) == (FixStatus.FAILED,
                                                   "The intake stage set status.")  # fmt: skip


async def test_a_stage_without_its_handler_says_so(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)
    pipeline = _pipeline(db_session, fake_redis, default_handlers())
    assert await pipeline.run_stage(run.org_id, run.id) is FixStage.INVESTIGATE
    assert await pipeline.run_stage(run.org_id, run.id) is None
    await db_session.refresh(run)
    assert (run.status, run.stop_reason) == (FixStatus.FAILED,
                                             "The investigate stage isn't built yet.")  # fmt: skip
