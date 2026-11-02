"""Fix intake over HTTP (8.3): a pasted trace or a GitHub issue starts a fix (members, not
viewers), a repository's fixes are listed and read (another org's look like none), a paste is
previewed; and Sentry: the org's integration (admins; secrets never shown back) and its webhook
(signature checked; busy issues start, the rest wait for triage; nothing started twice)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

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
from sensei_core.github.oauth import GitHubTokens
from sensei_core.mail import MemoryMailer
from sensei_core.models import FixRun, Membership, Repo, RepoSource, Role, SentryIntegration, User
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
TRACE = """\
Traceback (most recent call last):
  File "/srv/app/services/pricing.py", line 12, in unit_price
    return total / quantity
ZeroDivisionError: division by zero
"""
SECRET = "sentry-client-secret"


@pytest.fixture
def app(api_app: FastAPI, migrated_test_db: Settings, fake_redis: FakeAsyncRedis,
        db_session: AsyncSession) -> FastAPI:  # fmt: skip
    github = GitHubSettings(client_id="Iv23.test", client_secret=SecretStr("shh"))
    api_app.state.settings = migrated_test_db.model_copy(update={"github": github})
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)

    @asynccontextmanager
    async def same_session() -> AsyncGenerator[AsyncSession]:
        yield db_session  # the webhook's own sessions see the test's (rolled back) data

    api_app.state.stream_sessions = same_session
    return api_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        app.state.http = http
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test:
            yield test


async def _org(app: FastAPI, db: AsyncSession, slug: str = "acme") -> tuple[User, Repo]:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "http://localhost:3000").create(
        db, owner, name=slug.title(), slug=slug
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


async def _member(db: AsyncSession, repo: Repo, role: Role) -> User:
    user = await make_user(db)
    db.add(Membership(org_id=repo.org_id, user_id=user.id, role=role))
    await db.flush()
    return user


async def test_a_pasted_trace_starts_a_fix_members_only(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, repo = await _org(app, db_session)
    _, headers = await sign_in(app, client, db_session, owner)
    made = await client.post(
        f"{API}/orgs/acme/repos/items/fixes", json={"source": "paste", "text": TRACE},
        headers=headers,
    )  # fmt: skip
    assert made.status_code == 201, made.text
    fix = made.json()
    assert (fix["status"], fix["stage"], fix["source"]) == ("queued", "intake", "paste")
    assert fix["title"] == "ZeroDivisionError: division by zero"
    assert fix["ticket"]["error_type"] == "ZeroDivisionError"
    assert fix["ticket"]["frames"][0]["line"] == 12
    listed = (await client.get(f"{API}/orgs/acme/repos/items/fixes")).json()
    assert [f["id"] for f in listed] == [fix["id"]]
    assert (await client.get(f"{API}/orgs/acme/repos/items/fixes?status=failed")).json() == []
    got = await client.get(f"{API}/orgs/acme/fixes/{fix['id']}")
    assert (got.status_code, got.json()["ticket"]["message"]) == (200, "division by zero")
    # A viewer reads fixes but can't start one.
    viewer = await _member(db_session, repo, Role.VIEWER)
    _, theirs = await sign_in(app, client, db_session, viewer)
    refused = await client.post(
        f"{API}/orgs/acme/repos/items/fixes", json={"source": "paste", "text": TRACE},
        headers=theirs,
    )  # fmt: skip
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "insufficient_role")
    assert (await client.get(f"{API}/orgs/acme/fixes/{fix['id']}")).status_code == 200
    bad = await client.post(
        f"{API}/orgs/acme/repos/items/fixes", json={"source": "paste", "text": " "}, headers=theirs
    )
    assert bad.status_code == 422


async def test_another_orgs_fix_looks_like_none(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await _org(app, db_session)
    _, headers = await sign_in(app, client, db_session, owner)
    fix = (
        await client.post(
            f"{API}/orgs/acme/repos/items/fixes",
            json={"source": "paste", "text": TRACE},
            headers=headers,
        )
    ).json()
    stranger, _ = await _org(app, db_session, slug="other")
    await sign_in(app, client, db_session, stranger)
    gone = await client.get(f"{API}/orgs/other/fixes/{fix['id']}")
    assert (gone.status_code, gone.json()["error"]["code"]) == (404, "fix_not_found")


async def test_a_paste_is_previewed_and_nothing_is_saved(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await _org(app, db_session)
    _, headers = await sign_in(app, client, db_session, owner)
    preview = await client.post(
        f"{API}/orgs/acme/repos/items/fixes/parse", json={"text": TRACE}, headers=headers
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["language"] == "python"
    assert preview.json()["frames"][0]["function"] == "unit_price"
    assert preview.json()["culprit"] is None  # never indexed: no file of its to point at
    assert (await db_session.scalars(select(FixRun))).all() == []


async def test_a_github_issue_starts_a_fix_once(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await _org(app, db_session)
    _, headers = await sign_in(app, client, db_session, owner)
    issue = {
        "number": 5,
        "title": "Checkout crashes with an empty cart",
        "html_url": "https://github.com/o/items/issues/5",
        "body": f"Steps...\n\n```\n{TRACE}```",
    }
    with respx.mock(assert_all_called=False) as github:
        route = github.get("https://api.github.com/repos/o/items/issues/5").respond(json=issue)
        github.get("https://api.github.com/repos/o/items/issues/5/comments").respond(
            json=[{"body": "Same here on Python 3.12.4"}]
        )
        github.get("https://api.github.com/repos/o/items/issues/6").respond(
            json={**issue, "number": 6, "pull_request": {"url": "x"}}
        )
        github.get("https://api.github.com/repos/o/items/issues/6/comments").respond(json=[])
        made = await client.post(
            f"{API}/orgs/acme/repos/items/fixes",
            json={"source": "github_issue", "issue": "https://github.com/o/items/issues/5"},
            headers=headers,
        )
        assert made.status_code == 201, made.text
        fix = made.json()
        assert fix["title"] == "Checkout crashes with an empty cart"
        assert fix["source_ref"] == "https://github.com/o/items/issues/5"
        assert fix["ticket"]["error_type"] == "ZeroDivisionError"
        assert fix["ticket"]["versions"] == {"python": "3.12.4"}  # from the comment
        assert route.calls.last.request.headers["authorization"] == "Bearer ghu_owner"
        again = await client.post(
            f"{API}/orgs/acme/repos/items/fixes", json={"source": "github_issue", "issue": "#5"},
            headers=headers,
        )  # fmt: skip
        assert (again.status_code, again.json()["id"]) == (200, fix["id"])  # already going
        pull = await client.post(
            f"{API}/orgs/acme/repos/items/fixes", json={"source": "github_issue", "issue": "6"},
            headers=headers,
        )  # fmt: skip
        assert pull.json()["error"]["code"] == "not_an_issue"
    elsewhere = await client.post(
        f"{API}/orgs/acme/repos/items/fixes",
        json={"source": "github_issue", "issue": "https://github.com/x/y/issues/1"},
        headers=headers,
    )
    assert elsewhere.json()["error"]["code"] == "issue_elsewhere"


def _signed(payload: dict[str, object], secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"Sentry-Hook-Signature": signature, "Sentry-Hook-Resource": "event_alert",
               "Content-Type": "application/json"}  # fmt: skip
    return body, headers


def _alert(issue_id: str) -> dict[str, object]:
    frames = [{"abs_path": "/srv/app/services/pricing.py", "lineno": 12,
               "function": "unit_price", "in_app": True}]  # fmt: skip
    return {"action": "triggered", "data": {"event": {
        "issue_id": issue_id, "title": "ZeroDivisionError: division by zero",
        "datetime": datetime.now(UTC).isoformat(), "platform": "python",
        "url": "https://sentry.io/api/0/projects/acme-sentry/backend/events/e1/",
        "exception": {"values": [{"type": "ZeroDivisionError", "value": "division by zero",
                                  "stacktrace": {"frames": frames}}]}}}}  # fmt: skip


async def test_sentry_is_connected_by_admins_and_its_webhook_starts_fixes(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, repo = await _org(app, db_session)
    member = await _member(db_session, repo, Role.MEMBER)
    _, theirs = await sign_in(app, client, db_session, member)
    setup = {"sentry_org": "acme-sentry", "token": "sntrys_secret_token", "client_secret": SECRET,
             "projects": {"backend": "items"}, "auto_run": True, "min_events": 10}  # fmt: skip
    refused = await client.put(f"{API}/orgs/acme/integrations/sentry", json=setup, headers=theirs)
    assert refused.status_code == 403
    _, headers = await sign_in(app, client, db_session, owner)
    connected = await client.put(f"{API}/orgs/acme/integrations/sentry", json=setup,
                                 headers=headers)  # fmt: skip
    assert connected.status_code == 200, connected.text
    shown = connected.json()
    assert shown["projects"] == {"backend": "items"}
    assert shown["webhook_url"].endswith("/api/v1/webhooks/sentry/acme")
    assert "token" not in shown
    assert SECRET not in connected.text
    stored = await db_session.scalar(select(SentryIntegration))
    assert stored is not None
    assert SECRET.encode() not in stored.client_secret_enc  # encrypted
    client.cookies.clear()

    # A forged delivery: refused before anything is read.
    body, forged = _signed(_alert("4501"), secret="not-the-secret")
    no = await client.post(f"{API}/webhooks/sentry/acme", content=body, headers=forged)
    assert no.status_code == 401
    unknown = await client.post(f"{API}/webhooks/sentry/nobody", content=body, headers=forged)
    assert unknown.status_code == 401  # the same answer: which orgs exist isn't said

    with respx.mock(assert_all_called=False) as sentry:
        base = "https://sentry.io/api/0/organizations/acme-sentry/issues"
        sentry.get(f"{base}/4501/").respond(json={"stats": {"14d": [[1, 30]]}})  # busy
        sentry.get(f"{base}/4502/").respond(json={"stats": {"14d": [[1, 2]]}})  # quiet
        body, signed = _signed(_alert("4501"))
        busy = await client.post(f"{API}/webhooks/sentry/acme", content=body, headers=signed)
        assert busy.status_code == 202
        body, signed = _signed(_alert("4502"))
        await client.post(f"{API}/webhooks/sentry/acme", content=body, headers=signed)
        body, signed = _signed(_alert("4501"))  # again, while the first is going
        await client.post(f"{API}/webhooks/sentry/acme", content=body, headers=signed)
    runs = (await db_session.scalars(select(FixRun).order_by(FixRun.created_at))).all()
    assert [(r.source_ref, r.status.value) for r in runs] == [
        ("4501", "queued"), ("4502", "needs_triage"),
    ]  # fmt: skip
    assert runs[0].created_by is None  # the org's, not a person's
    culprit = runs[0].ticket["frames"][0]
    assert (culprit["line"], culprit["function"]) == (12, "unit_price")
