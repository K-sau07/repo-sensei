"""What a fix's run tells people (8.10): its plan waiting (the requester and the admins, again
for a new revision), its pull request, a stop (each one: a retry that stops again says so
again), a Sentry fix's news to the admins, a change's to its own page, and nothing for a run a
person ended; the inboxes' streams told after the commit."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    FixKind,
    FixRisk,
    FixRun,
    FixSource,
    FixStage,
    FixStatus,
    Membership,
    Notification,
    NotificationKind,
    Repo,
    RepoSource,
    Role,
    User,
)
from sensei_core.notifications.service import inbox_stream
from sensei_core.orgs.service import OrgService
from sensei_fixer.budget import Budget
from sensei_fixer.events import FixOutbox
from sensei_fixer.notices import fix_notices
from sensei_fixer.pipeline import FixPipeline, cancel
from sensei_fixer.stages import Advance, Outcome, StageContext, StageHandler, Stop
from sensei_fixer.state import FixStateMachine
from sensei_fixer.sweep import sweep_stalled
from sensei_testing import make_user

pytestmark = pytest.mark.integration


async def _team(db: AsyncSession) -> tuple[Repo, User, User, User]:
    """An org (its owner), an admin, a member who asks for fixes, and its repository."""
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "http://localhost:3000").create(
        db, owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}"
    )
    await set_org_context(db, access.org.id)
    admin, member = await make_user(db), await make_user(db)
    db.add(Membership(org_id=access.org.id, user_id=admin.id, role=Role.ADMIN))
    db.add(Membership(org_id=access.org.id, user_id=member.id, role=Role.MEMBER))
    repo = Repo(
        org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=7, owner="o",
        name="items", full_name="o/items", slug="items", default_branch="main",
        visibility="public",
    )  # fmt: skip
    db.add(repo)
    await db.flush()
    return repo, owner, admin, member


async def _run(db: AsyncSession, repo: Repo, by: User | None, *, stage: FixStage,
               status: FixStatus = FixStatus.QUEUED, **extra: Any) -> FixRun:  # fmt: skip
    run = FixRun(
        org_id=repo.org_id, repo_id=repo.id, source=FixSource.PASTE if by else FixSource.SENTRY,
        title="KeyError: 'x'", ticket={"title": "KeyError: 'x'"}, status=status, stage=stage,
        created_by=by.id if by else None, **extra,
    )  # fmt: skip
    db.add(run)
    await db.flush()
    return run


def _pipeline(db: AsyncSession, redis: FakeAsyncRedis, handler: StageHandler) -> FixPipeline:
    @asynccontextmanager
    async def same() -> AsyncGenerator[AsyncSession]:
        yield db
        await db.flush()

    return FixPipeline(sessions=same, redis=redis, handlers=dict.fromkeys(FixStage, handler),
                       budget=Budget())  # fmt: skip


def _advance(**updates: Any) -> StageHandler:
    async def handler(_ctx: StageContext) -> Outcome:
        return Advance(updates)

    return handler


def _stop(status: FixStatus, reason: str) -> StageHandler:
    async def handler(_ctx: StageContext) -> Outcome:
        return Stop(status, reason)

    return handler


async def _inbox(db: AsyncSession, run: FixRun) -> list[tuple[uuid.UUID, NotificationKind, str]]:
    rows = await db.execute(
        select(Notification.user_id, Notification.kind, Notification.title)
        .where(Notification.org_id == run.org_id)
        .order_by(Notification.id)
    )
    return list(rows.tuples().all())


async def test_a_plan_waiting_tells_the_requester_and_the_admins_and_again_when_revised(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    repo, owner, admin, member = await _team(db_session)
    plan = {"summary": "Guard it", "revision": 1}
    run = await _run(db_session, repo, member, stage=FixStage.PLAN, risk=FixRisk.HIGH, plan=plan)

    await _pipeline(db_session, fake_redis, _advance()).run_stage(run.org_id, run.id)

    told = await _inbox(db_session, run)
    assert {user for user, _, _ in told} == {owner.id, admin.id, member.id}
    assert {(kind, title) for _, kind, title in told} == {
        (NotificationKind.APPROVAL_NEEDED, "Fix plan to approve: KeyError: 'x'")
    }
    item = await db_session.scalar(select(Notification).where(Notification.user_id == member.id))
    assert item is not None
    assert item.link == f"/{(await _slug(db_session, run))}/items/fixes/{run.id}"
    assert item.body == "In o/items, risk high."
    assert item.meta == {"risk": "high"}  # the inbox shows it beside the words
    # Its stream heard after the commit.
    entries = await fake_redis.xrange(inbox_stream(run.org_id, member.id))  # pyright: ignore[reportUnknownMemberType]
    assert [orjson.loads(e[1]["data"])["kind"] for e in entries] == ["approval_needed"]
    # Changes asked for: planned again (revision 2), it asks again.
    FixStateMachine(run).replan()
    run.plan = {**plan, "revision": 2}
    await db_session.flush()
    await _pipeline(db_session, fake_redis, _advance()).run_stage(run.org_id, run.id)
    titles = [title for user, _, title in await _inbox(db_session, run) if user == member.id]
    assert titles == ["Fix plan to approve: KeyError: 'x'",
                      "Fix plan to approve (revision 2): KeyError: 'x'"]  # fmt: skip


async def _slug(db: AsyncSession, run: FixRun) -> str:
    from sensei_core.models import Org  # noqa: PLC0415

    org = await db.get(Org, run.org_id)
    assert org is not None
    return org.slug


async def test_a_pull_request_and_each_stop_tell_the_requester(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    repo, _owner, _admin, member = await _team(db_session)
    pr = await _run(db_session, repo, member, stage=FixStage.PR)
    url = "https://github.com/o/items/pull/9"
    await _pipeline(db_session, fake_redis, _advance(pr_url=url)).run_stage(pr.org_id, pr.id)
    stopped = await _run(db_session, repo, member, stage=FixStage.REPRODUCE)
    reason = "No reproducing test."
    await _pipeline(db_session, fake_redis, _stop(FixStatus.NOT_REPRODUCED, reason)).run_stage(
        stopped.org_id, stopped.id
    )

    told = await _inbox(db_session, pr)
    assert {user for user, _, _ in told} == {member.id}
    assert [(kind, title) for _, kind, title in told] == [
        (NotificationKind.PR_OPENED, "Draft pull request opened: KeyError: 'x'"),
        (NotificationKind.FIX_FAILED, "Fix couldn't be reproduced: KeyError: 'x'"),
    ]
    # Retried, and it stops again (later): said again.
    FixStateMachine(stopped, lambda: datetime.now(UTC) + timedelta(minutes=5)).retry()
    stopped.stage = FixStage.REPRODUCE
    await db_session.flush()
    later = FixPipeline(
        sessions=_pipeline(db_session, fake_redis, _advance()).sessions, redis=fake_redis,
        handlers=dict.fromkeys(FixStage, _stop(FixStatus.NOT_REPRODUCED, reason)),
        budget=Budget(), clock=lambda: datetime.now(UTC) + timedelta(minutes=10),
    )  # fmt: skip
    await later.run_stage(stopped.org_id, stopped.id)
    assert [kind for _, kind, _ in await _inbox(db_session, pr)].count(
        NotificationKind.FIX_FAILED
    ) == 2


async def test_a_sentry_fix_tells_the_admins_and_a_change_links_to_its_page(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    repo, owner, admin, member = await _team(db_session)
    sentry = await _run(db_session, repo, None, stage=FixStage.REPRODUCE)
    await _pipeline(db_session, fake_redis, _stop(FixStatus.FAILED, "It broke.")).run_stage(
        sentry.org_id, sentry.id
    )
    assert {user for user, _, _ in await _inbox(db_session, sentry)} == {owner.id, admin.id}
    change = await _run(db_session, repo, member, stage=FixStage.PATCH, kind=FixKind.CHANGE)
    await _pipeline(db_session, fake_redis, _stop(FixStatus.BLOCKED, "A rule.")).run_stage(
        change.org_id, change.id
    )
    item = await db_session.scalar(select(Notification).where(Notification.user_id == member.id))
    assert item is not None
    assert item.title == "Change was blocked: KeyError: 'x'"
    assert item.link is not None
    assert item.link.endswith(f"/items/changes/{change.id}")
    assert item.body == "In o/items. A rule."


async def test_a_run_a_person_ended_tells_nobody(db_session: AsyncSession) -> None:
    repo, _owner, _admin, member = await _team(db_session)
    run = await _run(db_session, repo, member, stage=FixStage.REPRODUCE)

    events = cancel(run, by="@someone")
    told = await fix_notices(db_session, run, events)

    assert told == []
    assert await _inbox(db_session, run) == []


async def test_a_fix_the_sweep_fails_tells_its_requester(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    repo, _owner, _admin, member = await _team(db_session)
    run = await _run(db_session, repo, member, stage=FixStage.REPRODUCE, status=FixStatus.RUNNING)
    now = datetime.now(UTC) + timedelta(hours=3)

    swept = await sweep_stalled(db_session, now, time_limit_seconds=600)
    for outbox in swept.failed:
        await outbox.publish(fake_redis)

    assert [(user, kind) for user, kind, _ in await _inbox(db_session, run)] == [
        (member.id, NotificationKind.FIX_FAILED)
    ]
    assert await fake_redis.xlen(inbox_stream(run.org_id, member.id)) == 1


async def test_a_lost_stream_doesnt_lose_the_inbox_item(db_session: AsyncSession) -> None:
    repo, _owner, _admin, member = await _team(db_session)
    run = await _run(db_session, repo, member, stage=FixStage.REPRODUCE, status=FixStatus.RUNNING)
    machine = FixStateMachine(run)
    machine.stop(FixStatus.FAILED, "It broke.")
    outbox = FixOutbox(run.org_id, run.id, run.repo_id)
    await outbox.add_told(db_session, run, machine.events)

    class Down(FakeAsyncRedis):
        async def xadd(self, *_args: object, **_kwargs: object) -> Any:
            from redis.exceptions import ConnectionError as RedisDown  # noqa: PLC0415

            raise RedisDown("down")

    await outbox.publish(Down())  # says nothing, raises nothing
    assert len(await _inbox(db_session, run)) == 1
