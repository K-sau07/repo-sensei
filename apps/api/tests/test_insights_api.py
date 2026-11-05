"""An org's insights (10.6.1) over HTTP: questions by day and repository, how the answers went,
the modules they cite and each week's adoption; only in repositories the reader can read, and
costs for admins only."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.routers.insights import module_of
from sensei_core.db.tenancy import set_org_context
from sensei_core.fixes.tickets import parse_ticket
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AgentRun,
    Conversation,
    ConversationMessage,
    Diagram,
    DiagramKind,
    DiagramSource,
    FixKind,
    FixRun,
    FixSource,
    FixStage,
    FixStatus,
    Membership,
    MessageFeedback,
    Repo,
    RepoSource,
    Role,
    User,
)
from sensei_core.orgs.service import OrgService
from sensei_core.repos.access import RepoAccess
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
TRACE = "Traceback (most recent call last):\n  File \"/a/b.py\", line 3, in f\nKeyError: 'x'\n"


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


def _repo(org_id: Any, name: str, github_id: int) -> Repo:
    return Repo(
        org_id=org_id, source=RepoSource.GITHUB_APP, github_repo_id=github_id, owner="o",
        name=name, full_name=f"o/{name}", slug=name, default_branch="main", visibility="private",
    )  # fmt: skip


def _cites(*paths: str) -> list[dict[str, Any]]:
    return [{"type": "text-delta", "delta": "Here."}] + [
        {"type": "data-citation", "id": f"citation-{n}", "data": {"path": path}}
        for n, path in enumerate(paths, 1)
    ]


def test_a_module_is_a_files_first_two_folders() -> None:
    assert module_of("backend/app/api/routes/items.py") == "backend/app"
    assert module_of("src/main.ts") == "src"
    assert module_of("README.md") == ""


async def test_insights_count_what_the_reader_can_read_and_costs_for_admins(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = await sign_in(api_app, client, db_session)
    access = await api_app.state.org_service.create(
        db_session, owner, name="Acme", slug=f"acme-{owner.id.hex[:8]}"
    )
    org_id = access.org.id
    await set_org_context(db_session, org_id)
    items, secret = _repo(org_id, "items", 41), _repo(org_id, "secret", 42)
    db_session.add_all([items, secret])
    await db_session.flush()

    async def readable(_: RepoAccess, user: User, repos: list[Repo]) -> list[Repo]:
        return [repo for repo in repos if repo.slug != "secret"]

    monkeypatch.setattr(RepoAccess, "readable", readable)

    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    other = await make_user(db_session)
    talk = Conversation(org_id=org_id, repo_id=items.id, user_id=owner.id, title="Saving")
    theirs = Conversation(org_id=org_id, repo_id=items.id, user_id=other.id, title="Theirs")
    hidden = Conversation(org_id=org_id, repo_id=secret.id, user_id=owner.id, title="Hidden")
    db_session.add_all([talk, theirs, hidden])
    await db_session.flush()

    def said(
        conversation: Conversation, role: str, when: datetime, **kw: Any
    ) -> ConversationMessage:
        return ConversationMessage(org_id=org_id, conversation_id=conversation.id, role=role,
                                   parts=kw.pop("parts", []), created_at=when, **kw)  # fmt: skip

    cited = _cites("backend/app/crud.py", "backend/app/api/items.py", "README.md")
    good = said(talk, "assistant", now, parts=cited)
    unsure = said(theirs, "assistant", now, abstained=True)
    db_session.add_all([
        said(talk, "user", now), good,
        said(talk, "user", now),  # a follow-up counts as a question too
        said(theirs, "user", now), unsure,
        said(talk, "user", old), said(talk, "assistant", old, parts=_cites("old/x/y.py")),
        said(hidden, "user", now),
        said(hidden, "assistant", now, parts=_cites("secret/plans/x.py")),
    ])  # fmt: skip
    await db_session.flush()
    db_session.add_all([
        MessageFeedback(org_id=org_id, message_id=good.id, user_id=owner.id, kind="helpful"),
        MessageFeedback(org_id=org_id, message_id=unsure.id, user_id=other.id, kind="wrong_file"),
        AgentRun(org_id=org_id, repo_id=items.id, user_id=owner.id, kind="ask", mode="fast",
                 cost_usd=Decimal("0.25")),
        AgentRun(org_id=org_id, repo_id=secret.id, user_id=owner.id, kind="ask", mode="fast",
                 cost_usd=Decimal("9")),
        FixRun(org_id=org_id, repo_id=items.id, source=FixSource.PASTE, title="KeyError",
               ticket=parse_ticket(TRACE).model_dump(mode="json"), status=FixStatus.QUEUED,
               stage=FixStage.INTAKE, created_by=owner.id),
        FixRun(org_id=org_id, repo_id=items.id, source=FixSource.REQUEST, title="Add limits",
               kind=FixKind.CHANGE, ticket={"title": "Add limits", "text": "60 a minute."},
               status=FixStatus.QUEUED, stage=FixStage.INTAKE, created_by=owner.id),
        Diagram(org_id=org_id, repo_id=secret.id, commit_sha="a" * 40, title="Hidden",
                kind=DiagramKind.FLOWCHART, source=DiagramSource.QUESTION, spec={},
                verified_ratio=1.0),
    ])  # fmt: skip
    await db_session.flush()

    response = await client.get(f"{API}/orgs/{access.org.slug}/insights")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["days"]) == 30
    today = body["days"][-1]
    assert (today["questions"], today["people"]) == (3, 2)
    assert Decimal(today["cost_usd"]) == Decimal("0.25")  # not the hidden repository's
    assert sum(day["questions"] for day in body["days"]) == 3  # not the 40-day-old one
    assert [(r["slug"], r["questions"]) for r in body["repos"]] == [("items", 3)]
    assert body["answers"] == {
        "answered": 1, "abstained": 1, "helpful": 1, "not_helpful": {"wrong_file": 1},
    }  # fmt: skip
    week = body["weeks"][-1]
    assert (week["answered"], week["abstained"], week["helpful"], week["not_helpful"]) == (
        1,
        1,
        1,
        1,
    )
    assert (week["askers"], week["fixes"], week["changes"], week["diagrams"]) == (2, 1, 1, 0)
    # One answer cites two files in backend/app: once for the module; the root file is "".
    assert {(m["path"], m["cited"]) for m in body["modules"]} == {("backend/app", 1), ("", 1)}
    assert body["costs"] is True

    member = await make_user(db_session)
    db_session.add(Membership(org_id=org_id, user_id=member.id, role=Role.MEMBER))
    await db_session.flush()
    await sign_in(api_app, client, db_session, member)
    seen = (await client.get(f"{API}/orgs/{access.org.slug}/insights")).json()
    assert seen["costs"] is False
    assert seen["days"][-1]["cost_usd"] is None
    assert seen["repos"][0]["cost_usd"] is None
    assert seen["days"][-1]["questions"] == 3
