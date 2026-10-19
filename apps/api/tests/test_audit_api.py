"""The org audit endpoint, and account events written by the auth routes."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_user_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import AuditEvent
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture(autouse=True)
def mailer(api_app: FastAPI) -> MemoryMailer:
    memory = MemoryMailer()
    api_app.state.org_service = OrgService(memory, api_app.state.settings.web.origin)
    return memory


async def _org_with_invites(
    client: httpx.AsyncClient, headers: dict[str, str], slug: str, invites: int
) -> None:
    created = await client.post(f"{API}/orgs", json={"name": "Acme", "slug": slug}, headers=headers)
    assert created.status_code == 201
    for n in range(invites):
        response = await client.post(
            f"{API}/orgs/{slug}/invitations",
            json={"email": f"p{n}@example.com"},
            headers=headers,
        )
        assert response.status_code == 201


async def test_admins_page_through_the_audit_log(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await sign_in(api_app, api_client, db_session)
    await _org_with_invites(api_client, headers, "audit-org", 3)

    first = (await api_client.get(f"{API}/orgs/audit-org/audit", params={"limit": 2})).json()
    assert [e["action"] for e in first["items"]] == ["invitation.create"] * 2
    assert first["items"][0]["actor_login"] == owner.login
    assert first["items"][0]["meta"]["email"] == "p2@example.com"
    assert first["next_cursor"] == first["items"][-1]["id"]

    second = (
        await api_client.get(
            f"{API}/orgs/audit-org/audit", params={"limit": 2, "before": first["next_cursor"]}
        )
    ).json()
    assert [e["action"] for e in second["items"]] == ["invitation.create", "org.create"]
    assert second["next_cursor"] == second["items"][-1]["id"]

    last = (
        await api_client.get(
            f"{API}/orgs/audit-org/audit", params={"limit": 2, "before": second["next_cursor"]}
        )
    ).json()
    assert last == {"items": [], "next_cursor": None}


async def test_audit_log_filters_by_action(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await sign_in(api_app, api_client, db_session)
    await _org_with_invites(api_client, headers, "filter-org", 2)
    body = (
        await api_client.get(f"{API}/orgs/filter-org/audit", params={"action": "org.create"})
    ).json()
    assert [e["action"] for e in body["items"]] == ["org.create"]
    assert body["next_cursor"] is None

    bad = await api_client.get(f"{API}/orgs/filter-org/audit", params={"action": "org.nope"})
    assert bad.status_code == 422


async def test_members_below_admin_cannot_read_the_audit_log(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers = await sign_in(api_app, api_client, db_session)
    await _org_with_invites(api_client, owner_headers, "closed-org", 0)
    member = await make_user(db_session, email="member@example.com")
    invite = await api_client.post(
        f"{API}/orgs/closed-org/invitations",
        json={"email": "member@example.com", "role": "member"},
        headers=owner_headers,
    )
    token = invite.json()["link"].rsplit("/", 1)[1]
    _, member_headers = await sign_in(api_app, api_client, db_session, member)
    await api_client.post(f"{API}/invitations/{token}/accept", headers=member_headers)

    response = await api_client.get(f"{API}/orgs/closed-org/audit")
    assert response.status_code == 403
    assert response.json()["error"]["details"] == {"required_role": "admin", "your_role": "member"}


async def test_logout_everywhere_is_audited(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, headers = await sign_in(api_app, api_client, db_session)
    await api_app.state.session_service.create(db_session, user, ip=None, user_agent="other")
    response = await api_client.post(
        f"{API}/auth/logout-all", headers={**headers, "X-Request-Id": "req-logout"}
    )
    assert response.status_code == 204

    await set_user_context(db_session, user.id)
    [event] = (await db_session.scalars(select(AuditEvent))).all()
    assert (event.action, event.org_id, event.actor_user_id) == ("auth.logout_all", None, user.id)
    assert (event.meta, event.request_id) == ({"sessions_revoked": 2}, "req-logout")
    assert event.ip is not None
