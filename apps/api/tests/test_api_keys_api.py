"""API keys (7.1) over the API: made (shown once), listed and revoked by their owner (or an
admin); a key in ``Authorization: Bearer`` acts as its user, in its org only, where routers let
keys in and within its scopes; revoked, expired and wrong keys are refused."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.auth import api_keys as keys_module
from sensei_core.auth.api_keys import ApiKeyService
from sensei_core.mail import MemoryMailer
from sensei_core.models import ActorType, AuditEvent, Membership, Repo, RepoSource, Role, User
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

API = "/api/v1"
pytestmark = pytest.mark.integration


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def _org(db: AsyncSession, owner: User, slug: str) -> None:
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name=slug.title(), slug=slug
    )
    db.add(
        Repo(
            org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=7, owner="o",
            name="items", full_name="o/items", slug="items", default_branch="main",
            visibility="public",
        )
    )  # fmt: skip
    await db.flush()


async def _forget_context(db: AsyncSession) -> None:
    """As a new request's transaction: no org, user or key chosen yet (the tests share one)."""
    for name in ("app.org_id", "app.user_id", "app.api_key_prefix"):
        await db.execute(text("SELECT set_config(:n, '', true)"), {"n": name})


async def _key(
    client: httpx.AsyncClient, headers: dict[str, str], org: str, **body: Any
) -> dict[str, Any]:
    made = await client.post(
        f"{API}/orgs/{org}/api-keys",
        json={"name": "laptop", "scopes": ["read:repo"], **body},
        headers=headers,
    )
    assert made.status_code == 201, made.text
    return made.json()


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_a_key_is_shown_once_and_listed_by_its_owner_or_an_admin(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session)
    await _org(db_session, owner, "acme")
    member = await make_user(db_session)
    org_id = await db_session.scalar(text("SELECT id FROM orgs WHERE slug = 'acme'"))
    db_session.add(Membership(org_id=org_id, user_id=member.id, role=Role.MEMBER))
    await db_session.flush()

    _, as_member = await sign_in(api_app, client, db_session, member)
    made = await _key(client, as_member, "acme", scopes=["ask", "read:repo"], expires_in_days=90)
    assert made["key"].startswith(f"rs_{made['prefix']}_")
    assert made["scopes"] == ["ask", "read:repo"]
    assert made["expires_at"] is not None
    listed = (await client.get(f"{API}/orgs/acme/api-keys")).json()["keys"]
    assert [k["id"] for k in listed] == [made["id"]]
    assert "key" not in listed[0]  # never again

    _, as_owner = await sign_in(api_app, client, db_session, owner)
    own = await _key(client, as_owner, "acme")
    everyone = (await client.get(f"{API}/orgs/acme/api-keys")).json()["keys"]
    assert {k["id"] for k in everyone} == {made["id"], own["id"]}  # an admin sees all
    # A member can't revoke someone else's key; an admin can.
    _, as_member = await sign_in(api_app, client, db_session, member)
    other = await client.delete(f"{API}/orgs/acme/api-keys/{own['id']}", headers=as_member)
    assert (other.status_code, other.json()["error"]["code"]) == (404, "api_key_not_found")
    _, as_owner = await sign_in(api_app, client, db_session, owner)
    gone = await client.delete(f"{API}/orgs/acme/api-keys/{made['id']}", headers=as_owner)
    assert gone.status_code == 204
    actions = (
        await db_session.scalars(
            select(AuditEvent.action).where(AuditEvent.target_type == "api_key")
        )
    ).all()
    assert sorted(actions) == ["api_key.create", "api_key.create", "api_key.revoke"]

    # What is asked for is checked.
    for body, code in (
        ({"scopes": ["admin"]}, "invalid_scope"),
        ({"scopes": ["read:repo"], "expires_in_days": 7}, "invalid_expiry"),
        ({"name": "   "}, "invalid_name"),
    ):
        bad = await client.post(
            f"{API}/orgs/acme/api-keys",
            json={"name": "x", "scopes": ["read:repo"], **body},
            headers=as_owner,
        )
        assert (bad.status_code, bad.json()["error"]["code"]) == (422, code), body


async def test_a_key_acts_as_its_user_where_keys_are_let_in_within_its_scopes(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session)
    await _org(db_session, owner, "acme")
    await _org(db_session, owner, "other")
    _, headers = await sign_in(api_app, client, db_session, owner)
    reader = (await _key(client, headers, "acme"))["key"]
    client.cookies.clear()
    await _forget_context(db_session)

    me = await client.get(f"{API}/me", headers=_bearer(reader))
    assert (me.status_code, me.json()["login"]) == (200, owner.login)
    # The user is in "other" too, but a key for "acme" shows only "acme".
    assert [o["slug"] for o in me.json()["orgs"]] == ["acme"]
    grep = await client.post(
        f"{API}/orgs/acme/repos/items/grep", json={"pattern": "x"}, headers=_bearer(reader)
    )
    assert grep.status_code != 403, grep.text  # a read that is a POST, allowed by name
    repos = await client.get(f"{API}/orgs/acme/repos", headers=_bearer(reader))
    assert repos.status_code == 200, repos.text
    assert [r["slug"] for r in repos.json()] == ["items"]
    # A read that is a POST, allowed by name (and no CSRF: a key isn't a cookie).
    searched = await client.post(
        f"{API}/orgs/acme/repos/items/search", json={"q": "items"}, headers=_bearer(reader)
    )
    assert searched.status_code != 403, searched.text

    def refused(response: httpx.Response, code: str) -> bool:
        return response.status_code in (401, 403, 404) and response.json()["error"]["code"] == code

    # Another org: as if there were none.
    assert refused(
        await client.get(f"{API}/orgs/other/repos", headers=_bearer(reader)), "org_not_found"
    )
    # Not the key's scope: asking needs `ask`.
    asked = await client.post(
        f"{API}/orgs/acme/repos/items/ask", json={"question": "what?"}, headers=_bearer(reader)
    )
    assert refused(asked, "api_key_scope")
    # Connecting needs connect:repo, which keys are never given.
    imported = await client.post(
        f"{API}/orgs/acme/repos/import", json={"url": "https://github.com/o/r"},
        headers=_bearer(reader),
    )  # fmt: skip
    assert refused(imported, "api_key_scope")
    # Writes keys aren't let do, and routers that refuse keys.
    for method, path in (
        ("DELETE", "/orgs/acme/repos/items"),
        ("GET", "/orgs/acme/api-keys"),
        ("GET", "/orgs/acme/members"),
        ("GET", "/me/sessions"),
        ("GET", "/orgs/acme/events"),  # a browser's stream, kept while its session lives
        ("GET", "/orgs/acme/inbox"),  # a person's, not a script's
        ("GET", "/me/notifications"),
        ("PUT", "/orgs/acme/integrations/slack"),
        ("POST", "/auth/logout"),
    ):
        response = await client.request(method, f"{API}{path}", headers=_bearer(reader), json={})
        assert refused(response, "api_key_not_allowed"), (method, path, response.text)


async def test_revoked_expired_and_wrong_keys_are_refused_and_their_timing_says_nothing(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await make_user(db_session)
    await _org(db_session, owner, "acme")
    _, headers = await sign_in(api_app, client, db_session, owner)
    revoked = await _key(client, headers, "acme")
    await client.delete(f"{API}/orgs/acme/api-keys/{revoked['id']}", headers=headers)
    expiring = await _key(client, headers, "acme", expires_in_days=30)
    client.cookies.clear()

    checks: list[str] = []
    real = keys_module._verify  # pyright: ignore[reportPrivateUsage]

    def counted(key_hash: str, key: str) -> bool:
        checks.append(key)
        return real(key_hash, key)

    monkeypatch.setattr(keys_module, "_verify", counted)

    async def code_for(key: str) -> tuple[int, str]:
        await _forget_context(db_session)
        response = await client.get(f"{API}/me", headers=_bearer(key))
        return response.status_code, response.json()["error"]["code"]

    assert await code_for(revoked["key"]) == (401, "api_key_revoked")
    wrong_secret = expiring["key"][:-4] + "zzzz"
    unknown_prefix = "rs_ZZZZZZZZ_" + expiring["key"].split("_")[2]
    for key in (wrong_secret, unknown_prefix, "rs_short"):
        assert await code_for(key) == (401, "api_key_invalid"), key
    # Every one hashed, found or not: the time doesn't tell which prefixes exist.
    assert checks == [revoked["key"], wrong_secret, unknown_prefix, "rs_short"]

    later = datetime.now(UTC) + timedelta(days=31)
    api_app.state.api_key_service = ApiKeyService(clock=lambda: later)
    assert await code_for(expiring["key"]) == (401, "api_key_expired")


async def test_a_keys_use_is_noted_now_and_then_and_its_actions_are_the_keys(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session)
    await _org(db_session, owner, "acme")
    _, headers = await sign_in(api_app, client, db_session, owner)
    key = (await _key(client, headers, "acme"))["key"]
    client.cookies.clear()
    start = datetime.now(UTC)
    for minutes, noted in ((0, 0), (2, 0), (6, 6)):
        now = start + timedelta(minutes=minutes)
        api_app.state.api_key_service = ApiKeyService(clock=lambda now=now: now)
        await _forget_context(db_session)
        assert (await client.get(f"{API}/me", headers=_bearer(key))).status_code == 200
        _, headers = await sign_in(api_app, client, db_session, owner)
        listed = (await client.get(f"{API}/orgs/acme/api-keys")).json()["keys"][0]
        client.cookies.clear()
        assert datetime.fromisoformat(listed["last_used_at"]) == start + timedelta(minutes=noted)


async def test_an_action_through_a_key_is_audited_as_the_keys(db_session: AsyncSession) -> None:
    import structlog  # noqa: PLC0415

    from sensei_core.audit import AuditAction, audit  # noqa: PLC0415

    user = await make_user(db_session)
    with structlog.contextvars.bound_contextvars(actor_type=ActorType.API_KEY.value):
        event = await audit(db_session, AuditAction.LOGIN, actor_user_id=user.id)
    plain = await audit(db_session, AuditAction.LOGIN, actor_user_id=user.id)
    rows = await db_session.execute(
        select(AuditEvent.id, AuditEvent.actor_type).where(AuditEvent.id.in_([event, plain]))
    )
    kinds = {row.id: row.actor_type for row in rows}
    assert (kinds[event], kinds[plain]) == (ActorType.API_KEY, ActorType.USER)


async def test_a_member_who_leaves_takes_no_access_with_their_keys(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session)
    await _org(db_session, owner, "acme")
    member = await make_user(db_session)
    org_id = await db_session.scalar(text("SELECT id FROM orgs WHERE slug = 'acme'"))
    db_session.add(Membership(org_id=org_id, user_id=member.id, role=Role.MEMBER))
    await db_session.flush()
    _, headers = await sign_in(api_app, client, db_session, member)
    key = (await _key(client, headers, "acme"))["key"]
    client.cookies.clear()
    await _forget_context(db_session)
    assert (await client.get(f"{API}/orgs/acme/repos", headers=_bearer(key))).status_code == 200
    await db_session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": member.id})
    await _forget_context(db_session)
    gone = await client.get(f"{API}/orgs/acme/repos", headers=_bearer(key))
    assert (gone.status_code, gone.json()["error"]["code"]) == (404, "org_not_found")
