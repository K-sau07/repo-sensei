"""Org and account lifecycle (10.7.3, F19) over HTTP: a deleted org is restorable by its owners
for a grace period; an account is deleted only once someone else owns its orgs, and then it is
anonymous."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.auth.github_login import upsert_user
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.oauth import GitHubUser
from sensei_core.mail import MemoryMailer
from sensei_core.models import Membership, OAuthToken, Org, Role, User
from sensei_core.orgs.lifecycle import DELETED_LOGIN, ORG_GRACE
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture(autouse=True)
def _orgs(api_app: FastAPI) -> None:
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)


async def _org(client: httpx.AsyncClient, headers: dict[str, str], slug: str) -> None:
    made = await client.post(
        f"{API}/orgs", json={"name": slug.title(), "slug": slug}, headers=headers
    )
    assert made.status_code == 201, made.text


async def test_a_deleted_org_is_restored_by_its_owner_within_the_grace(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await sign_in(api_app, api_client, db_session)
    await _org(api_client, headers, "gone-soon")
    assert (await api_client.delete(f"{API}/orgs/gone-soon", headers=headers)).status_code == 204
    assert (await api_client.get(f"{API}/orgs/gone-soon")).status_code == 404
    (deleted,) = (await api_client.get(f"{API}/orgs/deleted")).json()
    assert deleted["slug"] == "gone-soon"
    when = datetime.fromisoformat(deleted["deleted_at"])
    assert datetime.fromisoformat(deleted["purge_at"]) - when == ORG_GRACE

    # Someone else, a member even, can't bring it back.
    org = await db_session.scalar(select(Org).where(Org.slug == "gone-soon"))
    assert org is not None
    member = await make_user(db_session)
    await set_org_context(db_session, org.id)
    db_session.add(Membership(org_id=org.id, user_id=member.id, role=Role.ADMIN))
    await db_session.flush()
    _, theirs = await sign_in(api_app, api_client, db_session, member)
    refused = await api_client.post(f"{API}/orgs/gone-soon/restore", headers=theirs)
    assert refused.status_code == 404

    _, headers = await sign_in(api_app, api_client, db_session, owner)
    restored = await api_client.post(f"{API}/orgs/gone-soon/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    assert (await api_client.get(f"{API}/orgs/gone-soon")).status_code == 200
    assert (await api_client.get(f"{API}/orgs/deleted")).json() == []

    # Past its grace it is purged, not restored.
    assert (await api_client.delete(f"{API}/orgs/gone-soon", headers=headers)).status_code == 204
    await db_session.refresh(org)
    org.deleted_at = datetime.now(UTC) - ORG_GRACE - timedelta(minutes=1)
    await db_session.flush()
    assert (await api_client.get(f"{API}/orgs/deleted")).json() == []
    late = await api_client.post(f"{API}/orgs/gone-soon/restore", headers=headers)
    assert late.status_code == 404


async def test_an_account_is_deleted_once_its_orgs_have_another_owner(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, headers = await sign_in(api_app, api_client, db_session)
    await _org(api_client, headers, "mine-alone")
    db_session.add(OAuthToken(user_id=user.id, provider="github", access_token_enc=b"x"))
    await db_session.flush()
    login = user.login

    async def delete(confirm: str) -> httpx.Response:
        return await api_client.request(
            "DELETE", f"{API}/me", json={"confirm": confirm}, headers=headers
        )

    mistyped = await delete("someone-else")
    assert mistyped.status_code == 422
    assert mistyped.json()["error"]["code"] == "confirmation_mismatch"
    sole = await delete(login)
    assert sole.status_code == 409
    assert sole.json()["error"]["details"] == {"orgs": ["mine-alone"]}

    org = await db_session.scalar(select(Org).where(Org.slug == "mine-alone"))
    assert org is not None
    other = await make_user(db_session)
    await set_org_context(db_session, org.id)
    db_session.add(Membership(org_id=org.id, user_id=other.id, role=Role.OWNER))
    await db_session.flush()
    github_id = user.github_id

    deleted = await delete(login.upper())  # any case
    assert deleted.status_code == 204, deleted.text
    assert (await api_client.get(f"{API}/me")).status_code == 401  # its session ended
    await db_session.refresh(user)
    assert user.deleted_at is not None
    assert (user.login, user.name, user.email, user.avatar_url) == (DELETED_LOGIN, None, None, None)
    assert user.github_id < 0
    assert await db_session.scalar(select(OAuthToken).where(OAuthToken.user_id == user.id)) is None
    await set_org_context(db_session, org.id)
    members = list(
        await db_session.scalars(select(Membership.user_id).where(Membership.org_id == org.id))
    )
    assert members == [other.id]  # the org goes on, with its other owner

    # The same GitHub account signs up again: someone new, nothing of the old one.
    again = await upsert_user(db_session, GitHubUser(id=github_id, login=login, name=None,
                                                     email=None, avatar_url=None))  # fmt: skip
    assert again.id != user.id
    assert (await db_session.get(User, user.id)) is not None


async def test_an_api_key_or_token_cannot_delete_the_account(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await sign_in(api_app, api_client, db_session)
    api_client.cookies.clear()
    refused = await api_client.request(
        "DELETE", f"{API}/me", json={"confirm": "x"}, headers={"Authorization": "Bearer rsk_x"}
    )
    assert refused.status_code in {401, 403}
