"""An organization's home (4.13) through the API: its readable repositories, the agents running
in them now, and the signed-in user's own latest questions."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import AgentRun, Conversation, Repo, RepoSource
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def test_the_home_has_repositories_live_agents_and_the_users_questions(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, _ = await sign_in(api_app, client, db_session)
    access = await api_app.state.org_service.create(
        db_session, user, name="Acme", slug=f"acme-{user.id.hex[:8]}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=41,
        owner="o",
        name="items",
        full_name="o/items",
        slug="items",
        default_branch="main",
        visibility="public",
    )
    db_session.add(repo)
    await db_session.flush()
    other = await make_user(db_session)
    running = AgentRun(
        org_id=repo.org_id, repo_id=repo.id, user_id=other.id, kind="ask", mode="ask",
        question="Where are items saved?",
    )  # fmt: skip
    cut_off = AgentRun(
        org_id=repo.org_id, repo_id=repo.id, user_id=other.id, kind="ask", mode="ask",
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )  # fmt: skip
    finished = AgentRun(
        org_id=repo.org_id, repo_id=repo.id, user_id=user.id, kind="ask", mode="ask",
        status="succeeded", finished_at=datetime.now(UTC),
    )  # fmt: skip
    mine = Conversation(org_id=repo.org_id, repo_id=repo.id, user_id=user.id, title="Saving")
    theirs = Conversation(org_id=repo.org_id, repo_id=repo.id, user_id=other.id, title="Theirs")
    db_session.add_all([running, cut_off, finished, mine, theirs])
    await db_session.flush()

    response = await client.get(f"{API}/orgs/{access.org.slug}/home")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["slug"] for r in body["repos"]] == ["items"]
    # Running now; not one left "running" hours ago by a process that died.
    assert [(r["id"], r["user"]) for r in body["live"]] == [(str(running.id), other.login)]
    assert [c["title"] for c in body["recent"]] == ["Saving"]  # only the user's own


async def test_an_empty_org_has_an_empty_home(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, _ = await sign_in(api_app, client, db_session)
    access = await api_app.state.org_service.create(
        db_session, user, name="Empty", slug=f"empty-{user.id.hex[:8]}"
    )
    body = (await client.get(f"{API}/orgs/{access.org.slug}/home")).json()
    assert body == {"repos": [], "live": [], "recent": []}
