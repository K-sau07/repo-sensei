"""Org isolation over HTTP (10.10): the owner of org A reaches nothing of org B's.

Org B has one of everything an org has (a repository, index runs, a conversation with its answer
and agent run, a diagram, a fix and a change, a member, an invitation, an API key, audit events,
usage, Sentry and Slack set up), each named with a marker only B uses. A's owner (the most anyone
is in their own org) asks for them every way the API names them: through B's slug, and by B's
ids through A's own slug and repository. Every answer must be 404, never 403 (a 403 would say the
thing exists), and no answer, not even an error's message, may carry one of B's markers. A's own
listings mustn't either. Then B's owner finds everything as it was. A key for org A, held by
someone in both orgs, doesn't open B either.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.sharing import new_share_token, share_token_hash
from sensei_core.auth.api_keys import ApiKeyService
from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AgentRun,
    ApiKey,
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
    IndexJob,
    Invitation,
    JobKind,
    JobStatus,
    Membership,
    Repo,
    RepoSource,
    Role,
    SentryIntegration,
    SlackIntegration,
    UsageEvent,
    UsageKind,
    User,
)
from sensei_core.notifications.deliver import slack_context
from sensei_core.orgs.service import OrgAccess, OrgService
from sensei_core.security.crypto import TokenCipher
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"


class Recorder:
    """Background work the API asks for: recorded, never sent."""

    def __getattr__(self, name: str) -> Any:
        async def record(*args: Any, **kwargs: Any) -> bool:
            return True

        return record


@pytest.fixture
async def app(
    api_app: FastAPI, fake_redis: FakeAsyncRedis, db_session: AsyncSession
) -> AsyncGenerator[FastAPI]:
    state = api_app.state
    state.redis = fake_redis
    state.org_service = OrgService(MemoryMailer(), state.settings.web.origin)
    for name in ("repo_jobs", "galaxy_jobs", "fix_jobs", "webhook_dispatcher"):
        setattr(state, name, Recorder())

    @asynccontextmanager
    async def same_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    state.stream_sessions = same_session
    with respx.mock(assert_all_called=False) as outside:
        outside.route().respond(503)  # nothing leaves the process
        async with httpx.AsyncClient() as http:
            state.http = http
            yield api_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@dataclass
class Tenant:
    access: OrgAccess
    repo: Repo
    member: User | None = None
    ids: dict[str, uuid.UUID] | None = None

    @property
    def slug(self) -> str:
        return self.access.org.slug


async def _tenant(app: FastAPI, db: AsyncSession, owner: User, name: str) -> Tenant:
    service: OrgService = app.state.org_service
    access = await service.create(db, owner, name=name, slug=name.lower())
    await set_org_context(db, access.org.id)
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=secrets.randbelow(10**9),
        owner=name.lower(),
        name=f"{name.lower()}-repo",
        full_name=f"{name.lower()}/{name.lower()}-repo",
        slug=f"{name.lower()}-repo",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()
    return Tenant(access, repo)


async def _fill(app: FastAPI, db: AsyncSession, b: Tenant, mark: str) -> dict[str, uuid.UUID]:
    """One of everything in org B, each named with ``mark``."""
    org_id, owner = b.access.org.id, b.access.user
    await set_org_context(db, org_id)
    member = await make_user(db, login=f"{mark}-member")
    db.add(Membership(org_id=org_id, user_id=member.id, role=Role.MEMBER))
    b.member = member
    job = IndexJob(
        org_id=org_id,
        repo_id=b.repo.id,
        kind=JobKind.FULL,
        status=JobStatus.FAILED,
        error={"message": f"{mark}-error"},
        finished_at=datetime.now(UTC),
    )
    talk = Conversation(
        org_id=org_id,
        repo_id=b.repo.id,
        user_id=owner.id,
        title=f"{mark}-talk",
        share_token_hash=share_token_hash(new_share_token()),
    )
    run = AgentRun(
        org_id=org_id,
        repo_id=b.repo.id,
        user_id=owner.id,
        kind="ask",
        mode="fast",
        question=f"{mark}-question",
        status="succeeded",
    )
    diagram = Diagram(
        org_id=org_id,
        repo_id=b.repo.id,
        commit_sha="b" * 40,
        title=f"{mark}-diagram",
        kind=DiagramKind.FLOWCHART,
        source=DiagramSource.QUESTION,
        spec={},
        verified_ratio=1.0,
        created_by=owner.id,
    )
    plan: dict[str, Any] = {"summary": mark, "revision": 1, "files": [], "risk": {"level": "low"}}
    fix = FixRun(
        org_id=org_id,
        repo_id=b.repo.id,
        kind=FixKind.FIX,
        source=FixSource.PASTE,
        title=f"{mark}-fix",
        ticket={"title": mark},
        status=FixStatus.NEEDS_TRIAGE,
        stage=FixStage.INTAKE,
        created_by=owner.id,
    )
    change = FixRun(
        org_id=org_id,
        repo_id=b.repo.id,
        kind=FixKind.CHANGE,
        source=FixSource.REQUEST,
        title=f"{mark}-change",
        ticket={"title": mark},
        status=FixStatus.AWAITING_APPROVAL,
        stage=FixStage.AWAITING_APPROVAL,
        plan=plan,
        created_by=owner.id,
    )
    cipher: TokenCipher = app.state.token_cipher
    sentry = SentryIntegration(
        org_id=org_id,
        sentry_org=f"{mark}-sentry",
        token_enc=cipher.encrypt_text("t", context=str(org_id)),
        client_secret_enc=cipher.encrypt_text("s", context=str(org_id)),
    )
    slack = SlackIntegration(
        org_id=org_id,
        channel=f"{mark}-channel",
        webhook_url_enc=cipher.encrypt_text(
            "https://hooks.slack.com/services/T/B/x", context=slack_context(org_id)
        ),
    )
    usage = UsageEvent(
        org_id=org_id,
        user_id=owner.id,
        kind=UsageKind.LLM,
        provider="anthropic",
        model=f"{mark}-model",
        tokens_in=1,
        tokens_out=1,
        cost_usd=Decimal("2.5"),
        price_version="t",
    )
    db.add_all([job, talk, run, diagram, fix, change, sentry, slack, usage])
    await db.flush()
    message = ConversationMessage(
        org_id=org_id,
        conversation_id=talk.id,
        role="assistant",
        parts=[{"type": "text", "text": f"{mark}-answer"}],
        run_id=run.id,
    )
    db.add(message)
    service: OrgService = app.state.org_service
    invitation, _ = await service.invite(
        db, b.access, role=Role.ADMIN, github_login=f"{mark}-invitee"
    )
    keys: ApiKeyService = app.state.api_key_service
    key, _ = await keys.create(
        db, b.access, name=f"{mark}-key", scopes=["read:repo"], expires_in_days=None
    )
    await db.flush()
    return {
        "job": job.id,
        "conversation": talk.id,
        "message": message.id,
        "run": run.id,
        "diagram": diagram.id,
        "fix": fix.id,
        "change": change.id,
        "member": member.id,
        "invitation": invitation.id,
        "key": key.id,
    }


@dataclass
class Orgs:
    a: Tenant
    b: Tenant
    mark: str
    ids: dict[str, uuid.UUID]


@pytest.fixture
async def orgs(app: FastAPI, db_session: AsyncSession) -> Orgs:
    tag = secrets.token_hex(3)
    a = await _tenant(app, db_session, await make_user(db_session), f"Alpha{tag}")
    b = await _tenant(app, db_session, await make_user(db_session), f"Beta{tag}")
    mark = f"bmark{tag}"
    ids = await _fill(app, db_session, b, mark)
    return Orgs(a, b, mark, ids)


async def _send(
    client: httpx.AsyncClient, method: str, url: str, headers: dict[str, str], body: Any = None
) -> httpx.Response:
    """A request, given up on (failing) if it doesn't answer: a stream that starts hangs."""
    send = client.request(method, url, headers=headers, json=body)
    return await asyncio.wait_for(send, timeout=10)


def _by_slug(o: Orgs) -> list[tuple[str, str, Any]]:
    """B's things through B's slug."""
    org, repo, ids = f"{API}/orgs/{o.b.slug}", f"{API}/orgs/{o.b.slug}/repos/{o.b.repo.slug}", o.ids
    return [
        ("GET", org, None),
        ("PATCH", org, {"name": "Taken"}),
        ("DELETE", org, None),
        ("GET", f"{org}/members", None),
        ("PATCH", f"{org}/members/{ids['member']}", {"role": "viewer"}),
        ("DELETE", f"{org}/members/{ids['member']}", None),
        ("GET", f"{org}/invitations", None),
        ("POST", f"{org}/invitations", {"github_login": "me", "role": "owner"}),
        ("DELETE", f"{org}/invitations/{ids['invitation']}", None),
        ("GET", f"{org}/audit", None),
        ("GET", f"{org}/usage", None),
        ("GET", f"{org}/insights", None),
        ("GET", f"{org}/home", None),
        ("GET", f"{org}/inbox", None),
        ("GET", f"{org}/settings/models", None),
        ("PATCH", f"{org}/settings/models", {}),
        ("GET", f"{org}/integrations/sentry", None),
        ("DELETE", f"{org}/integrations/sentry", None),
        ("GET", f"{org}/integrations/slack", None),
        ("GET", f"{org}/api-keys", None),
        ("DELETE", f"{org}/api-keys/{ids['key']}", None),
        ("GET", f"{org}/repos", None),
        ("GET", repo, None),
        ("PATCH", repo, {"settings": {}}),
        ("DELETE", repo, None),
        ("GET", f"{repo}/index-jobs", None),
        ("GET", f"{repo}/index-jobs/{ids['job']}", None),
        ("POST", f"{repo}/reindex", None),
        ("GET", f"{repo}/conversations", None),
        ("GET", f"{org}/conversations/{ids['conversation']}", None),
        ("PATCH", f"{org}/conversations/{ids['conversation']}", {"title": "Mine"}),
        ("DELETE", f"{org}/conversations/{ids['conversation']}", None),
        ("POST", f"{org}/messages/{ids['message']}/feedback", {"kind": "helpful"}),
        ("POST", f"{repo}/ask", {"question": "What is this?"}),
        ("GET", f"{repo}/runs", None),
        ("GET", f"{org}/agent-runs/{ids['run']}", None),
        ("GET", f"{repo}/diagrams", None),
        ("GET", f"{repo}/diagrams/{ids['diagram']}", None),
        ("DELETE", f"{repo}/diagrams/{ids['diagram']}", None),
        ("GET", f"{repo}/fixes", None),
        ("GET", f"{org}/fixes/{ids['fix']}", None),
        ("POST", f"{org}/fixes/{ids['fix']}/cancel", None),
        ("GET", f"{repo}/changes", None),
        ("GET", f"{org}/changes/{ids['change']}", None),
        (
            "POST",
            f"{org}/changes/{ids['change']}/approval",
            {"decision": "approve", "plan_revision": 1},
        ),
    ]


def _by_id(o: Orgs) -> list[tuple[str, str, Any]]:
    """B's things by their ids, through A's own slug and repository."""
    org, repo, ids = f"{API}/orgs/{o.a.slug}", f"{API}/orgs/{o.a.slug}/repos/{o.a.repo.slug}", o.ids
    return [
        ("GET", f"{API}/orgs/{o.a.slug}/repos/{o.b.repo.slug}", None),
        ("PATCH", f"{org}/members/{ids['member']}", {"role": "viewer"}),
        ("DELETE", f"{org}/members/{ids['member']}", None),
        ("DELETE", f"{org}/invitations/{ids['invitation']}", None),
        ("DELETE", f"{org}/api-keys/{ids['key']}", None),
        ("GET", f"{repo}/index-jobs/{ids['job']}", None),
        ("POST", f"{repo}/index-jobs/{ids['job']}/cancel", None),
        ("GET", f"{repo}/index-jobs/{ids['job']}/events", None),
        ("GET", f"{org}/conversations/{ids['conversation']}", None),
        ("PATCH", f"{org}/conversations/{ids['conversation']}", {"title": "Mine"}),
        ("DELETE", f"{org}/conversations/{ids['conversation']}", None),
        ("POST", f"{org}/conversations/{ids['conversation']}/share", None),
        ("POST", f"{org}/messages/{ids['message']}/feedback", {"kind": "helpful"}),
        ("DELETE", f"{org}/messages/{ids['message']}/feedback", None),
        ("GET", f"{repo}/ask/messages/{ids['message']}/stream", None),
        ("GET", f"{org}/agent-runs/{ids['run']}", None),
        ("GET", f"{org}/agent-runs/{ids['run']}/events", None),
        ("GET", f"{repo}/diagrams/{ids['diagram']}", None),
        ("PATCH", f"{repo}/diagrams/{ids['diagram']}", {"title": "Mine"}),
        ("DELETE", f"{repo}/diagrams/{ids['diagram']}", None),
        ("POST", f"{repo}/diagrams/{ids['diagram']}/share", None),
        ("GET", f"{org}/fixes/{ids['fix']}", None),
        ("POST", f"{org}/fixes/{ids['fix']}/start", None),
        ("POST", f"{org}/fixes/{ids['fix']}/cancel", None),
        ("GET", f"{org}/fixes/{ids['fix']}/approvals", None),
        ("GET", f"{org}/fixes/{ids['fix']}/events", None),
        ("GET", f"{org}/changes/{ids['change']}", None),
        (
            "PATCH",
            f"{org}/changes/{ids['change']}/plan",
            {"plan_revision": 1, "edits": {"steps": [{"description": "x", "path": "a.py"}]}},
        ),
        ("POST", f"{org}/changes/{ids['change']}/cancel", None),
        (
            "POST",
            f"{org}/changes/{ids['change']}/approval",
            {"decision": "approve", "plan_revision": 1},
        ),
        ("GET", f"{org}/changes/{ids['change']}/events", None),
        ("POST", f"{API}/internal/admin/jobs/{ids['job']}/retry", None),  # staff's, not A's
    ]


def _leaks(o: Orgs, response: httpx.Response) -> list[str]:
    text = response.text
    found = [o.mark] if o.mark in text else []
    found += [str(v) for v in o.ids.values() if str(v) in text]
    found += [s for s in (o.b.slug, o.b.repo.full_name, str(o.b.access.org.id)) if s in text]
    return found


async def test_b_through_its_own_slug_is_not_there(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, orgs: Orgs
) -> None:
    _, headers = await sign_in(app, client, db_session, orgs.a.access.user)
    wrong: list[str] = []
    for method, url, body in _by_slug(orgs):
        response = await _send(client, method, url, headers, body)
        code = response.json().get("error", {}).get("code")
        if (response.status_code, code) != (404, "org_not_found"):
            wrong.append(f"{method} {url}: {response.status_code} {code}")
        if leaks := [m for m in _leaks(orgs, response) if m != orgs.b.slug]:  # its own URL's
            wrong.append(f"{method} {url} shows {leaks}")
    assert not wrong, "\n".join(wrong)


async def test_b_by_its_ids_through_a_is_not_there(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, orgs: Orgs
) -> None:
    _, headers = await sign_in(app, client, db_session, orgs.a.access.user)
    wrong: list[str] = []
    for method, url, body in _by_id(orgs):
        response = await _send(client, method, url, headers, body)
        if response.status_code != 404:
            code = response.json().get("error", {}).get("code")
            wrong.append(f"{method} {url}: {response.status_code} {code}")
        if leaks := _leaks(orgs, response):
            wrong.append(f"{method} {url} shows {leaks}")
    assert not wrong, "\n".join(wrong)


async def test_a_listings_show_nothing_of_b(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, orgs: Orgs
) -> None:
    _, headers = await sign_in(app, client, db_session, orgs.a.access.user)
    org, repo = f"{API}/orgs/{orgs.a.slug}", f"{API}/orgs/{orgs.a.slug}/repos/{orgs.a.repo.slug}"
    wrong: list[str] = []
    for url in (
        f"{API}/orgs",
        f"{API}/me",
        org,
        f"{org}/members",
        f"{org}/invitations",
        f"{org}/audit",
        f"{org}/usage",
        f"{org}/insights",
        f"{org}/home",
        f"{org}/inbox",
        f"{org}/api-keys",
        f"{org}/repos",
        f"{repo}/index-jobs",
        f"{repo}/conversations",
        f"{repo}/runs",
        f"{repo}/diagrams",
        f"{repo}/fixes",
        f"{repo}/changes",
        f"{org}/settings/models",
    ):
        response = await _send(client, "GET", url, headers)
        if response.status_code != 200:
            wrong.append(f"GET {url}: {response.status_code} {response.text[:200]}")
        if leaks := _leaks(orgs, response):
            wrong.append(f"GET {url} shows {leaks}")
    assert not wrong, "\n".join(wrong)


async def test_b_finds_everything_as_it_was(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, orgs: Orgs
) -> None:
    """After A's attempts, B's owner sees it all, unchanged (and the markers are really there:
    the checks above would see them if they leaked)."""
    _, a_headers = await sign_in(app, client, db_session, orgs.a.access.user)
    for method, url, body in [*_by_slug(orgs), *_by_id(orgs)]:
        await _send(client, method, url, a_headers, body)
    _, headers = await sign_in(app, client, db_session, orgs.b.access.user)
    org, repo = f"{API}/orgs/{orgs.b.slug}", f"{API}/orgs/{orgs.b.slug}/repos/{orgs.b.repo.slug}"
    ids = orgs.ids
    for url, needle in (
        (org, orgs.b.access.org.name),
        (f"{org}/members", f"{orgs.mark}-member"),
        (f"{org}/invitations", f"{orgs.mark}-invitee"),
        (f"{org}/api-keys", f"{orgs.mark}-key"),
        (f"{org}/usage", f"{orgs.mark}-model"),
        (f"{org}/conversations/{ids['conversation']}", f"{orgs.mark}-talk"),
        (f"{org}/agent-runs/{ids['run']}", f"{orgs.mark}-question"),
        (f"{repo}/diagrams/{ids['diagram']}", f"{orgs.mark}-diagram"),
        (f"{org}/fixes/{ids['fix']}", f"{orgs.mark}-fix"),
        (f"{org}/changes/{ids['change']}", f"{orgs.mark}-change"),
        (f"{org}/integrations/sentry", f"{orgs.mark}-sentry"),
        (f"{org}/integrations/slack", f"{orgs.mark}-channel"),
        (f"{repo}/index-jobs/{ids['job']}", str(ids["job"])),
    ):
        response = await _send(client, "GET", url, headers)
        assert response.status_code == 200, (url, response.text)
        assert needle in response.text, url
    await set_org_context(db_session, orgs.b.access.org.id)
    assert orgs.b.member is not None
    role = await db_session.scalar(
        select(Membership.role).where(
            Membership.org_id == orgs.b.access.org.id, Membership.user_id == orgs.b.member.id
        )
    )
    assert role is Role.MEMBER
    invitation = await db_session.get(Invitation, ids["invitation"], populate_existing=True)
    key = await db_session.get(ApiKey, ids["key"], populate_existing=True)
    fix = await db_session.get(FixRun, ids["fix"], populate_existing=True)
    change = await db_session.get(FixRun, ids["change"], populate_existing=True)
    assert invitation is not None
    assert invitation.revoked_at is None
    assert key is not None
    assert key.revoked_at is None
    assert fix is not None
    assert fix.status is FixStatus.NEEDS_TRIAGE
    assert change is not None
    assert change.status is FixStatus.AWAITING_APPROVAL


async def test_a_key_for_a_doesnt_open_b_for_someone_in_both(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, orgs: Orgs
) -> None:
    both = await make_user(db_session)
    for tenant in (orgs.a, orgs.b):
        await set_org_context(db_session, tenant.access.org.id)
        db_session.add(Membership(org_id=tenant.access.org.id, user_id=both.id, role=Role.ADMIN))
        await db_session.flush()
    keys: ApiKeyService = app.state.api_key_service
    await set_org_context(db_session, orgs.a.access.org.id)
    a_access = OrgAccess(org=orgs.a.access.org, role=Role.ADMIN, user=both)
    _, token = await keys.create(
        db_session, a_access, name="ci", scopes=["read:repo", "ask"], expires_in_days=None
    )
    headers = {"Authorization": f"Bearer {token}"}
    client.cookies.clear()
    repo_a = f"{API}/orgs/{orgs.a.slug}/repos/{orgs.a.repo.slug}"
    assert (await _send(client, "GET", repo_a, headers)).status_code == 200
    org, repo = f"{API}/orgs/{orgs.b.slug}", f"{API}/orgs/{orgs.b.slug}/repos/{orgs.b.repo.slug}"
    for method, url, body in (
        ("GET", f"{org}/repos", None),
        ("GET", repo, None),
        ("GET", f"{repo}/index-jobs/{orgs.ids['job']}", None),
        ("GET", f"{repo}/runs", None),
        ("GET", f"{org}/agent-runs/{orgs.ids['run']}", None),
        ("POST", f"{repo}/search", {"query": "x"}),
        ("POST", f"{repo}/ask", {"question": "What is this?"}),
    ):
        response = await _send(client, method, url, headers, body)
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "org_not_found",
        ), url
        assert not _leaks(orgs, response) or _leaks(orgs, response) == [orgs.b.slug], url
