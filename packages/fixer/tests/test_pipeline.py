"""Running a fix's stages (8.4) against the database: one stage per call and the next one
named, steps and spend recorded, events streamed; a stop, a failing stage, a cancel while a
stage works (its result dropped), the budget, and a duplicate delivery."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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
    FixKind,
    FixRun,
    FixSource,
    FixStage,
    FixStatus,
    Org,
    Repo,
    RepoSource,
)
from sensei_core.orgs.model_choices import RECOMMENDED, StepChoice
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
from sensei_fixer.sweep import sweep_stalled
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


async def test_a_fix_that_used_its_budget_up_takes_no_more_turns(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    budget = Budget(max_turns=3, max_cost_usd=Decimal(5), max_sandbox_seconds=600)
    turns: list[int] = []

    async def thrifty(ctx: StageContext) -> Outcome:
        for n in range(1, 10):
            await ctx.check()  # before each turn
            turns.append(n)
            await ctx.spend(1, Decimal("0.01"), 0.0)
        return Advance()

    run = await _fix(db_session)
    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: thrifty}, budget)
    assert await pipeline.run_stage(run.org_id, run.id) is None
    await db_session.refresh(run)
    assert turns == [1, 2, 3]  # not a fourth
    assert (run.status, run.stop_reason) == (
        FixStatus.FAILED,
        "Stopped: it used its 3 agent turns.",
    )
    exact = await _fix(db_session)  # all of it, and done: fine
    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: _advancing()},
                         Budget(max_turns=1))  # fmt: skip
    assert await pipeline.run_stage(exact.org_id, exact.id) is FixStage.INVESTIGATE


async def test_a_message_for_a_stage_the_fix_has_left_does_nothing(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)  # queued at intake
    calls: list[str] = []

    async def counted(_ctx: StageContext) -> Outcome:
        calls.append("ran")
        return Advance()

    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: counted})
    assert await pipeline.run_stage(run.org_id, run.id, FixStage.REPRODUCE) is None
    assert calls == []
    assert await pipeline.run_stage(run.org_id, run.id, FixStage.INTAKE) is FixStage.INVESTIGATE
    assert calls == ["ran"]


async def test_the_sweep_fails_lost_stages_and_sends_lost_messages_again(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    now = datetime.now(UTC)
    lost, waiting, forgotten, fresh = [await _fix(db_session) for _ in range(4)]
    await set_org_context(db_session, lost.org_id)  # RLS: the test's session is an org's
    agent = AgentRun(org_id=lost.org_id, repo_id=lost.repo_id, kind="fix", mode="fix",
                     question=lost.title)  # fmt: skip
    db_session.add(agent)
    await db_session.flush()
    lost.run_id, lost.status, lost.stage = agent.id, FixStatus.RUNNING, FixStage.INVESTIGATE
    ages = {lost: timedelta(seconds=2100 + 601), waiting: timedelta(minutes=11),
            forgotten: timedelta(hours=25), fresh: timedelta(minutes=2)}  # fmt: skip
    for run, age in ages.items():
        await set_org_context(db_session, run.org_id)
        run.status_at = now - age
        await db_session.flush()
    for run in (lost, waiting, forgotten, fresh):
        await set_org_context(db_session, run.org_id)  # RLS: the test's session is an org's
        swept = await sweep_stalled(db_session, now, time_limit_seconds=2100)
        await db_session.flush()  # where the sweep's session commits (before the next org's)
        for outbox in swept.failed:
            await outbox.publish(fake_redis)
        if run is waiting:
            assert swept.resend == [(run.org_id, run.id, "intake")]
        else:
            assert swept.resend == []
    for run in (lost, waiting, forgotten, fresh):
        await set_org_context(db_session, run.org_id)
        await db_session.refresh(run)
    assert lost.status is FixStatus.FAILED
    assert lost.stop_reason == (
        "The investigate stage's worker stopped before it finished (it was restarted, or ran "
        "out of time)."
    )
    assert (forgotten.status, forgotten.stop_reason) == (
        FixStatus.FAILED, "No worker took the intake stage in 24 hours.",
    )  # fmt: skip
    assert (waiting.status, fresh.status) == (FixStatus.QUEUED, FixStatus.QUEUED)
    await set_org_context(db_session, lost.org_id)
    await db_session.refresh(agent)
    assert (agent.status, agent.stopped, agent.finished_at) == ("failed", "failed", now)
    assert [e["type"] for e in await _events(fake_redis, lost.id)] == ["failed"]


async def test_a_change_goes_from_its_request_to_its_plan_then_on_as_a_fix_does(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)
    run.kind, run.source = FixKind.CHANGE, FixSource.REQUEST
    run.ticket = {"title": "Add rate limiting", "text": "60 requests a minute per client."}
    await db_session.flush()
    kinds: list[tuple[FixStage, FixKind]] = []

    def seen(**updates: Any) -> StageHandler:
        async def handler(ctx: StageContext) -> Outcome:
            kinds.append((ctx.stage, ctx.kind))
            return Advance(updates)

        return handler

    handlers = {stage: seen() for stage in FixStage}
    handlers[FixStage.PLAN] = seen(plan={"summary": "Limit", "revision": 1})
    pipeline = _pipeline(db_session, fake_redis, handlers)
    while await pipeline.run_stage(run.org_id, run.id) is not None:
        pass
    await db_session.refresh(run)
    assert run.status is FixStatus.AWAITING_APPROVAL
    FixStateMachine(run).approve()
    await db_session.flush()
    while await pipeline.run_stage(run.org_id, run.id) is not None:
        pass
    await db_session.refresh(run)
    assert run.status is FixStatus.SUCCEEDED
    # No investigation, reproduction or root cause; every stage told it is a change's.
    assert kinds == [(stage, FixKind.CHANGE) for stage in (
        FixStage.INTAKE, FixStage.PLAN, FixStage.PATCH, FixStage.VERIFY, FixStage.PR,
    )]  # fmt: skip
    agent = await db_session.get(AgentRun, run.run_id)
    assert agent is not None
    assert (agent.kind, agent.mode) == ("change", "change")


async def test_each_stage_is_told_the_orgs_floor_for_fixes(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    run = await _fix(db_session)
    org = await db_session.get(Org, run.org_id)
    assert org is not None
    floors: list[StepChoice] = []

    async def handler(ctx: StageContext) -> Outcome:
        floors.append(ctx.floor)
        return Advance({})

    pipeline = _pipeline(db_session, fake_redis, {FixStage.INTAKE: handler})
    await pipeline.run_stage(run.org_id, run.id)
    run.status, run.stage = FixStatus.QUEUED, FixStage.INTAKE
    org.model_choices = {"fixes": {"tier": "deep", "effort": "high"}}
    await db_session.flush()
    await pipeline.run_stage(run.org_id, run.id)
    assert floors == [RECOMMENDED, StepChoice(tier="deep", effort="high")]
