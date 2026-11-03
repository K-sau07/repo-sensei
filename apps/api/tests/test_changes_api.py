"""Requested changes over HTTP (9.1): a member starts one (viewers can't), it is a run of its
own kind (a change's routes find no fix, and a fix's no change; the lists keep them apart), and
its plan is decided on through its own route, by the fix's rules."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.auth.github_login import store_tokens
from sensei_core.config import GitHubSettings, Settings
from sensei_core.github.oauth import GitHubTokens
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    FixKind,
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


async def _org(app: FastAPI, db: AsyncSession) -> tuple[User, Repo]:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "http://localhost:3000").create(
        db, owner, name="Acme", slug="acme"
    )
    repo = Repo(
        org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=7, owner="o",
        name="items", full_name="o/items", slug="items", default_branch="main",
        visibility="public",
    )  # fmt: skip
    db.add(repo)
    await db.flush()
    await store_tokens(db, app.state.token_cipher, owner, GitHubTokens(
        access_token="ghu_owner", refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=8), refresh_expires_at=None, scopes=[],
    ))  # fmt: skip
    return owner, repo


async def test_a_member_starts_a_change_kept_apart_from_fixes(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, repo = await _org(app, db_session)
    viewer = await make_user(db_session)
    db_session.add(Membership(org_id=repo.org_id, user_id=viewer.id, role=Role.VIEWER))
    await db_session.flush()
    _, theirs = await sign_in(app, client, db_session, viewer)
    body = {"request": "Add rate limiting to the items API.\n\n60 a minute per client."}
    refused = await client.post(f"{API}/orgs/acme/repos/items/changes", json=body, headers=theirs)
    assert refused.status_code == 403
    _, headers = await sign_in(app, client, db_session, owner)
    made = await client.post(f"{API}/orgs/acme/repos/items/changes", json=body, headers=headers)
    assert made.status_code == 201, made.text
    change = made.json()
    assert (change["kind"], change["source"], change["status"], change["stage"]) == (
        "change", "request", "queued", "intake",
    )  # fmt: skip
    assert change["title"] == "Add rate limiting to the items API."
    assert change["ticket"]["frames"] == []
    assert change["author"] == owner.login
    assert app.state.fix_jobs.sent == [(change["id"], "intake")]
    # A fix beside it.
    fix = FixRun(
        org_id=repo.org_id, repo_id=repo.id, source=FixSource.PASTE, title="KeyError",
        ticket={"title": "KeyError"}, status=FixStatus.QUEUED, stage=FixStage.INTAKE,
    )  # fmt: skip
    db_session.add(fix)
    await db_session.flush()
    changes = (await client.get(f"{API}/orgs/acme/repos/items/changes")).json()
    fixes = (await client.get(f"{API}/orgs/acme/repos/items/fixes")).json()
    assert [c["id"] for c in changes] == [change["id"]]
    assert [f["id"] for f in fixes] == [str(fix.id)]
    got = await client.get(f"{API}/orgs/acme/changes/{change['id']}")
    assert (got.status_code, got.json()["ticket"]["text"]) == (200, body["request"])
    as_fix = await client.get(f"{API}/orgs/acme/fixes/{change['id']}")
    assert (as_fix.status_code, as_fix.json()["error"]["code"]) == (404, "fix_not_found")
    as_change = await client.get(f"{API}/orgs/acme/changes/{fix.id}")
    assert (as_change.status_code, as_change.json()["error"]["code"]) == (404, "change_not_found")
    long = {"request": "x" * 4_001}
    too_long = await client.post(f"{API}/orgs/acme/repos/items/changes", json=long,
                                 headers=headers)  # fmt: skip
    assert too_long.status_code == 422


async def test_a_changes_plan_is_decided_through_its_own_route(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, repo = await _org(app, db_session)
    run = FixRun(
        org_id=repo.org_id, repo_id=repo.id, kind=FixKind.CHANGE, source=FixSource.REQUEST,
        title="Add rate limiting", ticket={"title": "Add rate limiting", "text": "60/min"},
        status=FixStatus.AWAITING_APPROVAL, stage=FixStage.AWAITING_APPROVAL, risk=FixRisk.LOW,
        plan={"summary": "Limit", "revision": 1, "files": [], "risk": {"level": "low"}},
        created_by=owner.id,
    )  # fmt: skip
    db_session.add(run)
    await db_session.flush()
    _, headers = await sign_in(app, client, db_session, owner)
    with respx.mock(assert_all_called=False) as github:
        github.get("https://api.github.com/repositories/7").respond(
            json={"id": 7, "permissions": {"pull": True, "push": True}}
        )
        wrong = await client.post(f"{API}/orgs/acme/fixes/{run.id}/approval",
                                  json={"decision": "approve", "plan_revision": 1},
                                  headers=headers)  # fmt: skip
        assert wrong.status_code == 404  # a fix's route finds no change
        approved = await client.post(f"{API}/orgs/acme/changes/{run.id}/approval",
                                     json={"decision": "approve", "plan_revision": 1},
                                     headers=headers)  # fmt: skip
    assert approved.status_code == 200, approved.text
    assert (approved.json()["status"], approved.json()["stage"]) == ("queued", "patch")
    assert app.state.fix_jobs.sent == [(str(run.id), "patch")]
    listed = (await client.get(f"{API}/orgs/acme/changes/{run.id}/approvals")).json()
    assert [a["decision"] for a in listed] == ["approve"]
