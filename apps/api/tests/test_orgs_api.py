"""Org, member and invitation endpoints over HTTP."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture
def mailer(api_app: FastAPI) -> MemoryMailer:
    memory = MemoryMailer()
    api_app.state.org_service = OrgService(memory, api_app.state.settings.web.origin)
    return memory


async def _create_org(client: httpx.AsyncClient, headers: dict[str, str], slug: str) -> None:
    response = await client.post(
        f"{API}/orgs", json={"name": slug.title(), "slug": slug}, headers=headers
    )
    assert response.status_code == 201, response.text


async def test_create_and_list_orgs(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await sign_in(api_app, api_client, db_session)
    response = await api_client.post(
        f"{API}/orgs", json={"name": "Acme Inc", "slug": "acme-inc"}, headers=headers
    )
    assert response.status_code == 201
    assert response.json()["role"] == "owner"

    listed = (await api_client.get(f"{API}/orgs")).json()
    assert [o["slug"] for o in listed] == ["acme-inc"]
    me = (await api_client.get(f"{API}/me")).json()
    assert me["orgs"] == [{"slug": "acme-inc", "name": "Acme Inc", "role": "owner"}]
    assert me["last_org_slug"] == "acme-inc"


async def test_create_org_requires_csrf(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await sign_in(api_app, api_client, db_session)
    response = await api_client.post(f"{API}/orgs", json={"name": "X", "slug": "xyz-org"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


@pytest.mark.parametrize(
    ("slug", "available", "reason"),
    [
        ("fresh-name", True, None),
        ("taken-name", False, "slug_taken"),
        ("ab", False, "invalid_slug"),
    ],
)
async def test_slug_availability(
    slug: str,
    available: bool,
    reason: str | None,
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, headers = await sign_in(api_app, api_client, db_session)
    await _create_org(api_client, headers, "taken-name")
    body = (await api_client.get(f"{API}/orgs/slug-available", params={"slug": slug})).json()
    assert (body["available"], body["reason"]) == (available, reason)


async def test_non_members_get_not_found(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await sign_in(api_app, api_client, db_session)
    await _create_org(api_client, headers, "private-org")
    await sign_in(api_app, api_client, db_session)  # someone else
    for path in ("/orgs/private-org", "/orgs/private-org/members", "/orgs/no-such-org"):
        response = await api_client.get(f"{API}{path}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "org_not_found"


async def test_insufficient_role_explains_what_is_needed(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession, mailer: MemoryMailer
) -> None:
    owner, owner_headers = await sign_in(api_app, api_client, db_session)
    await _create_org(api_client, owner_headers, "role-check")
    viewer = await make_user(db_session, email="viewer@example.com")
    invite = await api_client.post(
        f"{API}/orgs/role-check/invitations",
        json={"email": "viewer@example.com", "role": "viewer"},
        headers=owner_headers,
    )
    token = invite.json()["link"].rsplit("/", 1)[1]

    _, viewer_headers = await sign_in(api_app, api_client, db_session, viewer)
    accepted = await api_client.post(f"{API}/invitations/{token}/accept", headers=viewer_headers)
    assert accepted.json()["role"] == "viewer"

    response = await api_client.patch(
        f"{API}/orgs/role-check", json={"name": "Hijacked"}, headers=viewer_headers
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "insufficient_role"
    assert error["details"] == {"required_role": "admin", "your_role": "viewer"}
    assert owner.id != viewer.id


async def test_invitation_flow_over_http(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession, mailer: MemoryMailer
) -> None:
    _, owner_headers = await sign_in(api_app, api_client, db_session)
    await _create_org(api_client, owner_headers, "team-org")
    created = await api_client.post(
        f"{API}/orgs/team-org/invitations",
        json={"email": "friend@example.com", "role": "member"},
        headers=owner_headers,
    )
    assert created.status_code == 201
    link = created.json()["link"]
    assert mailer.sent[0].link == link
    token = link.rsplit("/", 1)[1]

    pending = (await api_client.get(f"{API}/orgs/team-org/invitations")).json()
    assert [p["email"] for p in pending] == ["friend@example.com"]
    assert "link" not in pending[0]

    # The invitee can preview without being signed in.
    api_client.cookies.clear()
    preview = (await api_client.get(f"{API}/invitations/{token}")).json()
    assert (preview["org_slug"], preview["role"], preview["status"]) == (
        "team-org",
        "member",
        "pending",
    )

    friend = await make_user(db_session, email="friend@example.com")
    _, friend_headers = await sign_in(api_app, api_client, db_session, friend)
    accepted = await api_client.post(f"{API}/invitations/{token}/accept", headers=friend_headers)
    assert accepted.status_code == 200
    members = (await api_client.get(f"{API}/orgs/team-org/members")).json()
    assert sorted(m["role"] for m in members) == ["member", "owner"]


async def test_member_role_change_and_removal(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession, mailer: MemoryMailer
) -> None:
    owner, headers = await sign_in(api_app, api_client, db_session)
    await _create_org(api_client, headers, "people-org")
    teammate = await make_user(db_session, email="mate@example.com")
    invite = await api_client.post(
        f"{API}/orgs/people-org/invitations", json={"email": "mate@example.com"}, headers=headers
    )
    token = invite.json()["link"].rsplit("/", 1)[1]
    _, mate_headers = await sign_in(api_app, api_client, db_session, teammate)
    await api_client.post(f"{API}/invitations/{token}/accept", headers=mate_headers)

    _, headers = await sign_in(api_app, api_client, db_session, owner)
    promoted = await api_client.patch(
        f"{API}/orgs/people-org/members/{teammate.id}", json={"role": "admin"}, headers=headers
    )
    assert promoted.json()["role"] == "admin"

    removed = await api_client.delete(
        f"{API}/orgs/people-org/members/{teammate.id}", headers=headers
    )
    assert removed.status_code == 204

    last_owner = await api_client.post(f"{API}/orgs/people-org/leave", headers=headers)
    assert last_owner.status_code == 409
    assert last_owner.json()["error"]["code"] == "last_owner"


async def test_last_org_preference_requires_membership(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await sign_in(api_app, api_client, db_session)
    await _create_org(api_client, headers, "first-org")
    await _create_org(api_client, headers, "second-org")
    ok = await api_client.patch(f"{API}/me", json={"last_org_slug": "first-org"}, headers=headers)
    assert ok.json()["last_org_slug"] == "first-org"
    denied = await api_client.patch(
        f"{API}/me", json={"last_org_slug": "not-mine"}, headers=headers
    )
    assert denied.status_code == 404
