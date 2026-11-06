"""The staff admin console (10.6.3) over HTTP: the service today, people, orgs and every org's
index runs, a failed run retried and a running one cancelled as the org's owner would, and the
read-only switch; staff only. And the ``staff_read`` policy itself: SELECT only, and only for a
user the database says is staff."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction
from sensei_core.db.tenancy import set_org_context, set_staff_context
from sensei_core.limits import READ_ONLY
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AuditEvent,
    IndexJob,
    JobKind,
    JobStatus,
    Repo,
    RepoSource,
    UsageEvent,
    UsageKind,
    User,
)
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
ADMIN = f"{API}/internal/admin"


class Dispatched:
    """The background work the API asked for (nothing is sent anywhere)."""

    def __init__(self) -> None:
        self.indexed: list[uuid.UUID] = []
        self.revoked: list[str] = []

    async def index(self, org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID) -> None:
        self.indexed.append(job_id)

    async def revoke(self, task_ids: list[str]) -> None:
        self.revoked.extend(task_ids)


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.repo_jobs = Dispatched()
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def _two_orgs(api_app: FastAPI, db: AsyncSession) -> tuple[Repo, Repo, IndexJob]:
    """Two orgs (no staff in either), a repository each, and a failed run in the second."""
    service: OrgService = api_app.state.org_service
    repos: list[Repo] = []
    for n in range(2):
        owner = await make_user(db)
        access = await service.create(db, owner, name=f"Org {n}", slug=f"org{n}-{owner.id.hex[:8]}")
        await set_org_context(db, access.org.id)
        repo = Repo(
            org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=100 + n,
            owner="o", name=f"r{n}", full_name=f"o/r{n}", slug=f"r{n}", default_branch="main",
            visibility="public",
        )  # fmt: skip
        db.add(repo)
        db.add(UsageEvent(org_id=access.org.id, user_id=owner.id, kind=UsageKind.LLM,
                          provider="anthropic", model="claude-sonnet-5-5", tokens_in=1,
                          tokens_out=1, cost_usd=Decimal("1.5"), price_version="t"))  # fmt: skip
        await db.flush()
        repos.append(repo)
    failed = IndexJob(org_id=repos[1].org_id, repo_id=repos[1].id, kind=JobKind.FULL,
                      status=JobStatus.FAILED, error={"message": "clone failed"},
                      finished_at=datetime.now(UTC))  # fmt: skip
    db.add(failed)
    await db.flush()
    return repos[0], repos[1], failed


async def test_staff_see_every_org_and_retry_a_failed_run_as_its_owner(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    first, second, failed = await _two_orgs(api_app, db_session)
    staff = await make_user(db_session, is_staff=True)
    _, headers = await sign_in(api_app, client, db_session, staff)

    today = (await client.get(ADMIN)).json()
    assert Decimal(today["spent_today_usd"]) >= Decimal(3)  # both orgs' spend
    assert today["read_only"] is False
    assert today["failed_runs_today"] >= 1

    runs = (await client.get(f"{ADMIN}/jobs")).json()
    mine = next(r for r in runs if r["id"] == str(failed.id))
    assert (mine["repo_full_name"], mine["status"]) == ("o/r1", "failed")
    orgs = {o["id"]: o for o in (await client.get(f"{ADMIN}/orgs")).json()}
    assert orgs[str(second.org_id)]["repos"] == 1
    assert orgs[str(second.org_id)]["members"] == 1
    assert Decimal(orgs[str(second.org_id)]["spent_30d_usd"]) == Decimal("1.5")
    people = (await client.get(f"{ADMIN}/users", params={"q": staff.login})).json()
    assert [(p["login"], p["is_staff"], p["orgs"]) for p in people] == [(staff.login, True, 0)]

    retried = await client.post(f"{ADMIN}/jobs/{failed.id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    assert (retried.json()["status"], retried.json()["repo_id"]) == ("queued", str(second.id))
    assert api_app.state.repo_jobs.indexed == [uuid.UUID(retried.json()["id"])]
    again = await client.post(f"{ADMIN}/jobs/{retried.json()['id']}/retry", headers=headers)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "not_failed")
    cancelled = await client.post(f"{ADMIN}/jobs/{retried.json()['id']}/cancel", headers=headers)
    assert (cancelled.status_code, cancelled.json()["status"]) == (200, "cancelled")
    # Done in the run's org, by the staff member, and its audit log says staff did it.
    await set_org_context(db_session, second.org_id)
    actions = set(await db_session.scalars(select(AuditEvent.action).where(
        AuditEvent.org_id == second.org_id, AuditEvent.actor_user_id == staff.id,
    )))  # fmt: skip
    assert {AuditAction.STAFF_INDEX_RETRY.value, AuditAction.STAFF_INDEX_CANCEL.value} <= actions
    assert AuditAction.REPO_INDEX_STARTED.value in actions

    # A disconnected repository's failed run isn't retried (its page doesn't exist either).
    second.deleted_at = datetime.now(UTC)
    await db_session.flush()
    gone = await client.post(f"{ADMIN}/jobs/{failed.id}/retry", headers=headers)
    assert gone.status_code == 404
    assert first.org_id != second.org_id


async def test_staff_stop_new_spending_and_lift_it(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_redis: FakeAsyncRedis,
) -> None:
    staff = await make_user(db_session, is_staff=True)
    _, headers = await sign_in(api_app, client, db_session, staff)
    stopped = await client.put(f"{ADMIN}/read-only", json={"on": True}, headers=headers)
    assert stopped.json()["read_only"] is True
    assert await fake_redis.exists(READ_ONLY.format(day=datetime.now(UTC).date().isoformat()))
    lifted = await client.put(f"{ADMIN}/read-only", json={"on": False}, headers=headers)
    assert lifted.json()["read_only"] is False
    said = (await db_session.scalars(select(AuditEvent.meta).where(
        AuditEvent.action == AuditAction.SERVICE_READ_ONLY.value,
        AuditEvent.actor_user_id == staff.id,
    ).order_by(AuditEvent.id))).all()  # fmt: skip
    assert said == [{"on": True}, {"on": False}]


async def test_no_one_else_sees_the_console(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, _, failed = await _two_orgs(api_app, db_session)
    assert (await client.get(ADMIN)).status_code == 401
    _, headers = await sign_in(api_app, client, db_session)
    for path in ("", "/users", "/orgs", "/jobs"):
        response = await client.get(f"{ADMIN}{path}")
        assert response.status_code == 404, path
    retry = await client.post(f"{ADMIN}/jobs/{failed.id}/retry", headers=headers)
    assert retry.status_code == 404


async def test_the_policy_reads_only_and_only_for_staff(db_session: AsyncSession) -> None:
    service = OrgService(MemoryMailer(), "http://localhost:3000")
    repos: list[Repo] = []
    for n in range(2):
        owner = await make_user(db_session)
        access = await service.create(db_session, owner, name=f"P{n}",
                                      slug=f"p{n}-{owner.id.hex[:8]}")  # fmt: skip
        await set_org_context(db_session, access.org.id)
        repo = Repo(
            org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=200 + n,
            owner="o", name=f"p{n}", full_name=f"o/p{n}", slug=f"p{n}", default_branch="main",
            visibility="public",
        )  # fmt: skip
        db_session.add(repo)
        await db_session.flush()
        repos.append(repo)
    mine, theirs = repos
    await set_org_context(db_session, mine.org_id)
    ids = {mine.id, theirs.id}

    async def seen() -> set[uuid.UUID]:
        return set(await db_session.scalars(select(Repo.id).where(Repo.id.in_(ids))))

    assert await seen() == {mine.id}
    plain = await make_user(db_session)
    await set_staff_context(db_session, plain.id)  # named, but not staff: nothing more
    assert await seen() == {mine.id}
    staff = await make_user(db_session, is_staff=True)
    await set_staff_context(db_session, staff.id)
    assert await seen() == {mine.id, theirs.id}
    # Reading only: another org's row isn't changed.
    await db_session.execute(update(Repo).where(Repo.id == theirs.id).values(name="taken"))
    name = await db_session.scalar(
        select(Repo.name).where(Repo.id == theirs.id).execution_options(populate_existing=True)
    )
    assert name == "p1"
    user = await db_session.get(User, staff.id)
    assert user is not None
    assert user.is_staff
