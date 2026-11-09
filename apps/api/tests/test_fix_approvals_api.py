"""Decisions on a fix's plan (8.8) over HTTP: who may decide (members with write access on
GitHub, asked live), the two-person rule for a high-risk fix, a decision on a plan that changed
meanwhile, changes asked for (planned again; earlier approvals don't count), a rejection, and
every decision audited."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.auth.github_login import store_tokens
from sensei_core.config import GitHubSettings, Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.oauth import GitHubTokens
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AuditEvent,
    FixRisk,
    FixRun,
    FixSource,
    FixStage,
    FixStatus,
    Membership,
    Repo,
    RepoSource,
    Role,
    User,
)
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
REPO_API = "https://api.github.com/repositories/7"
PLAN = {
    "summary": "Guard the empty cart", "revision": 1, "risk": {"level": "low"},
    "steps": [{"description": "Return 0", "path": "services/pricing.py"}],
    "files": [{"path": "services/pricing.py", "change": "modify", "why": "the division"}],
}  # fmt: skip


class FakeFixJobs:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def run_stage(self, org_id: uuid.UUID, fix_id: uuid.UUID, stage: str) -> bool:
        self.sent.append((str(fix_id), stage))
        return True


@pytest.fixture
def app(api_app: FastAPI, migrated_test_db: Settings, fake_redis: FakeAsyncRedis,
        db_session: AsyncSession) -> FastAPI:  # fmt: skip
    github = GitHubSettings(client_id="Iv23.test", client_secret=SecretStr("shh"))
    api_app.state.settings = migrated_test_db.model_copy(update={"github": github})
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)

    @asynccontextmanager
    async def same_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    api_app.state.stream_sessions = same_session
    api_app.state.fix_jobs = FakeFixJobs()
    return api_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        app.state.http = http
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test:
            yield test


async def _token(app: FastAPI, db: AsyncSession, user: User) -> None:
    await store_tokens(db, app.state.token_cipher, user, GitHubTokens(
        access_token=f"ghu_{user.login}", refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=8), refresh_expires_at=None, scopes=[],
    ))  # fmt: skip


@pytest.fixture
async def team(app: FastAPI, db_session: AsyncSession) -> dict[str, Any]:
    """An org with a repository and people: the owner (who asks for fixes), two more members
    who can push to it on GitHub, a member who can only read it, and a viewer."""
    owner = await make_user(db_session)
    access = await OrgService(MemoryMailer(), "http://localhost:3000").create(
        db_session, owner, name="Acme", slug="acme"
    )
    repo = Repo(
        org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=7, owner="o",
        name="items", full_name="o/items", slug="items", default_branch="main",
        visibility="public",
    )  # fmt: skip
    db_session.add(repo)
    await db_session.flush()
    people: dict[str, Any] = {"owner": owner, "repo": repo}
    for name, role in (("ann", Role.MEMBER), ("bo", Role.MEMBER), ("reader", Role.MEMBER),
                       ("viewer", Role.VIEWER)):  # fmt: skip
        user = await make_user(db_session)
        db_session.add(Membership(org_id=repo.org_id, user_id=user.id, role=role))
        people[name] = user
    await db_session.flush()
    for user in (owner, people["ann"], people["bo"], people["reader"], people["viewer"]):
        await _token(app, db_session, user)
    people["writers"] = {f"Bearer ghu_{u.login}" for u in (owner, people["ann"], people["bo"])}
    return people


def _github(writers: set[str]) -> respx.MockRouter:
    """GitHub answering each user's own permissions on the repository, from their token."""
    router = respx.mock(assert_all_called=False)

    def permissions(request: httpx.Request) -> httpx.Response:
        push = request.headers.get("authorization") in writers
        return httpx.Response(200, json={"id": 7, "permissions": {"pull": True, "push": push}})

    router.get(REPO_API).mock(side_effect=permissions)
    return router


async def _awaiting(db: AsyncSession, repo: Repo, owner: User, risk: FixRisk) -> FixRun:
    run = FixRun(
        org_id=repo.org_id, repo_id=repo.id, source=FixSource.PASTE, title="ZeroDivisionError",
        ticket={"title": "ZeroDivisionError"}, status=FixStatus.AWAITING_APPROVAL,
        stage=FixStage.AWAITING_APPROVAL, plan={**PLAN, "risk": {"level": risk.value}},
        risk=risk, created_by=owner.id,
    )  # fmt: skip
    db.add(run)
    await db.flush()
    return run


async def _decide(app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, user: User,
                  run: FixRun, **body: Any) -> httpx.Response:  # fmt: skip
    _, headers = await sign_in(app, client, db, user)
    payload = {"decision": "approve", "plan_revision": 1, **body}
    return await client.post(f"{API}/orgs/acme/fixes/{run.id}/approval", json=payload,
                             headers=headers)  # fmt: skip


async def test_only_members_who_can_write_on_github_decide(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, team: dict[str, Any]
) -> None:
    run = await _awaiting(db_session, team["repo"], team["owner"], FixRisk.LOW)
    with _github(team["writers"]) as github:
        viewer = await _decide(app, client, db_session, team["viewer"], run)
        assert viewer.status_code == 403  # a viewer, whatever GitHub says
        reader = await _decide(app, client, db_session, team["reader"], run)
        assert (reader.status_code, reader.json()["error"]["code"]) == (
            403, "github_write_required",
        )  # fmt: skip
        approved = await _decide(app, client, db_session, team["ann"], run, comment="LGTM")
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert (body["status"], body["stage"], body["approvals_needed"]) == (
            "queued", "patch", 1,
        )  # fmt: skip
        assert app.state.fix_jobs.sent == [(str(run.id), "patch")]
        assert github.calls.call_count == 2  # the reader's and ann's, each asked live
    await set_org_context(db_session, team["repo"].org_id)
    audited = (await db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "fix.decision")
    )).all()  # fmt: skip
    assert [(e.actor_user_id, e.meta["decision"], e.meta["status"]) for e in audited] == [
        (team["ann"].id, "approve", "queued"),
    ]
    listed = (await client.get(f"{API}/orgs/acme/fixes/{run.id}/approvals")).json()
    assert [(a["login"], a["decision"], a["comment"], a["plan_revision"]) for a in listed] == [
        (team["ann"].login, "approve", "LGTM", 1),
    ]


async def test_a_high_risk_fix_needs_two_people_not_only_its_requester(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, team: dict[str, Any]
) -> None:
    run = await _awaiting(db_session, team["repo"], team["owner"], FixRisk.HIGH)
    with _github(team["writers"]):
        mine = await _decide(app, client, db_session, team["owner"], run)
        assert mine.status_code == 200, mine.text
        assert (mine.json()["status"], mine.json()["approvals_needed"]) == ("awaiting_approval", 2)
        again = await _decide(app, client, db_session, team["owner"], run)
        assert (again.status_code, again.json()["error"]["code"]) == (409, "fix_already_approved")
        assert app.state.fix_jobs.sent == []
        second = await _decide(app, client, db_session, team["ann"], run)
        assert (second.json()["status"], second.json()["stage"]) == ("queued", "patch")
    assert app.state.fix_jobs.sent == [(str(run.id), "patch")]


async def test_a_decision_on_a_plan_that_changed_is_refused(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, team: dict[str, Any]
) -> None:
    run = await _awaiting(db_session, team["repo"], team["owner"], FixRisk.LOW)
    with _github(team["writers"]):
        stale = await _decide(app, client, db_session, team["ann"], run, plan_revision=0)
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "fix_plan_changed")
        unsaid = await _decide(app, client, db_session, team["ann"], run,
                               decision="request_changes")  # fmt: skip
        assert (unsaid.status_code, unsaid.json()["error"]["code"]) == (422, "fix_changes_unsaid")
        edits = {"summary": "Raise a clear error instead"}
        sneaked = await _decide(app, client, db_session, team["ann"], run, plan_edits=edits)
        assert sneaked.json()["error"]["code"] == "fix_edits_need_replan"


async def test_changes_asked_for_plan_again_and_earlier_approvals_dont_count(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, team: dict[str, Any]
) -> None:
    run = await _awaiting(db_session, team["repo"], team["owner"], FixRisk.HIGH)
    with _github(team["writers"]):
        assert (await _decide(app, client, db_session, team["ann"], run)).status_code == 200
        edits = {"steps": [{"description": "Raise ValueError for an empty cart",
                            "path": "services/pricing.py"}]}  # fmt: skip
        asked = await _decide(app, client, db_session, team["bo"], run, decision="request_changes",
                              comment="Callers expect an error.", plan_edits=edits)  # fmt: skip
        assert asked.status_code == 200, asked.text
        assert (asked.json()["status"], asked.json()["stage"]) == ("queued", "plan")
        assert app.state.fix_jobs.sent == [(str(run.id), "plan")]
        [request] = asked.json()["plan"]["requests"]
        assert request == {"by": team["bo"].login, "comment": "Callers expect an error.",
                           "edits": edits, "revision": 1}  # fmt: skip
        # The planner made revision 2; it waits for approval again.
        await db_session.flush()
        await db_session.refresh(run)
        run.plan = {**PLAN, "revision": 2}
        run.status, run.stage = FixStatus.AWAITING_APPROVAL, FixStage.AWAITING_APPROVAL
        await db_session.flush()
        one = await _decide(app, client, db_session, team["bo"], run, plan_revision=2)
        assert one.json()["status"] == "awaiting_approval"  # ann approved revision 1, not 2


async def test_a_rejected_fix_stops_and_takes_no_more_decisions(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, team: dict[str, Any]
) -> None:
    run = await _awaiting(db_session, team["repo"], team["owner"], FixRisk.LOW)
    with _github(team["writers"]):
        rejected = await _decide(app, client, db_session, team["bo"], run, decision="reject",
                                 comment="We're removing this endpoint.")  # fmt: skip
        assert rejected.status_code == 200, rejected.text
        body = rejected.json()
        assert body["status"] == "rejected"
        assert (
            body["stop_reason"] == f"Rejected by @{team['bo'].login}: We're removing this endpoint."
        )
        late = await _decide(app, client, db_session, team["ann"], run)
        assert (late.status_code, late.json()["error"]["code"]) == (
            409,
            "fix_not_awaiting_approval",
        )
    assert app.state.fix_jobs.sent == []


async def test_github_not_answering_is_said_so_not_a_crash(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, team: dict[str, Any]
) -> None:
    run = await _awaiting(db_session, team["repo"], team["owner"], FixRisk.LOW)
    with respx.mock(assert_all_called=False) as github:
        github.get(REPO_API).mock(return_value=httpx.Response(502))
        down = await _decide(app, client, db_session, team["ann"], run)
    assert (down.status_code, down.json()["error"]["code"]) == (503, "github_unavailable")
    with respx.mock(assert_all_called=False) as github:
        github.get(REPO_API).mock(return_value=httpx.Response(401))
        lapsed = await _decide(app, client, db_session, team["ann"], run)
    assert (lapsed.status_code, lapsed.json()["error"]["code"]) == (
        401, "github_reauth_required",
    )  # fmt: skip
    await db_session.refresh(run)
    assert run.status is FixStatus.AWAITING_APPROVAL  # nothing was decided
