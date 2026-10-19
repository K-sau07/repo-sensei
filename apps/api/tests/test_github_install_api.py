"""Installing the GitHub App for an org, end to end against a mocked GitHub."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.auth.github_login import store_tokens
from sensei_core.config import GitHubSettings, Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.oauth import GitHubTokens
from sensei_core.mail import MemoryMailer
from sensei_core.models import AuditEvent, GitHubInstallation, Membership, Org, Role, User
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
GITHUB_API = "https://api.github.com"
INSTALLATION = 777_001

_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
)
GH_SETTINGS = GitHubSettings(
    app_id=99,
    app_slug="repo-sensei-test",
    client_id="Iv23.test",
    client_secret=SecretStr("shh"),
    private_key=SecretStr(_PEM),
)
INSTALLATION_BODY = {
    "id": INSTALLATION,
    "account": {"login": "acme-gh", "id": 5150, "type": "Organization"},
    "target_type": "Organization",
    "repository_selection": "selected",
    "permissions": {"contents": "read", "metadata": "read"},
    "events": ["push", "pull_request"],
    "suspended_at": None,
}


@pytest.fixture
def github_app(api_app: FastAPI, migrated_test_db: Settings, fake_redis: FakeAsyncRedis) -> FastAPI:
    api_app.state.settings = migrated_test_db.model_copy(update={"github": GH_SETTINGS})
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    return api_app


@pytest.fixture
async def client(github_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        github_app.state.http = http
        transport = httpx.ASGITransport(app=github_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test:
            yield test


@pytest.fixture
def web(github_app: FastAPI) -> str:
    origin: str = github_app.state.settings.web.origin
    return origin


async def _admin_with_org(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, slug: str = "acme"
) -> User:
    user, headers = await sign_in(app, client, db)
    response = await client.post(
        f"{API}/orgs", json={"name": slug.title(), "slug": slug}, headers=headers
    )
    assert response.status_code == 201, response.text
    await store_tokens(
        db,
        app.state.token_cipher,
        user,
        GitHubTokens(
            access_token=f"ghu_{user.login}",
            refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(hours=8),
            refresh_expires_at=None,
            scopes=[],
        ),
    )
    return user


async def _start(client: httpx.AsyncClient, slug: str = "acme", source: str = "repos") -> str:
    response = await client.get(f"{API}/github/install", params={"org": slug, "from": source})
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert (location.netloc, location.path) == (
        "github.com",
        "/apps/repo-sensei-test/installations/new",
    )
    return parse_qs(location.query)["state"][0]


MEMBERSHIP_URL = f"{GITHUB_API}/user/memberships/orgs/acme-gh"


def _github(
    router: respx.MockRouter,
    *,
    user_sees: list[int] | None = None,
    membership: httpx.Response | None = None,
) -> tuple[respx.Route, respx.Route]:
    """GitHub as an admin of the installing organization sees it (override to test refusals)."""
    router.get(MEMBERSHIP_URL).mock(
        return_value=membership
        or httpx.Response(200, json={"state": "active", "role": "admin", "user": {"id": 1}})
    )
    visible = [INSTALLATION] if user_sees is None else user_sees
    listing = router.get(f"{GITHUB_API}/user/installations").mock(
        return_value=httpx.Response(
            200,
            json={"total_count": len(visible), "installations": [{"id": i} for i in visible]},
        )
    )
    details = router.get(f"{GITHUB_API}/app/installations/{INSTALLATION}").mock(
        return_value=httpx.Response(200, json=INSTALLATION_BODY)
    )
    return listing, details


async def _setup(client: httpx.AsyncClient, state: str, **params: str | int) -> httpx.Response:
    query = {"installation_id": INSTALLATION, "setup_action": "install", "state": state, **params}
    return await client.get(f"{API}/github/setup", params=query)


def _error(response: httpx.Response) -> str | None:
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query).get("github_error", [None])[0]


def _csrf(client: httpx.AsyncClient) -> str:
    return next(v for k, v in client.cookies.items() if "csrf" in k)


async def _org_id(db: AsyncSession, slug: str) -> uuid.UUID:
    return (await db.scalars(select(Org.id).where(Org.slug == slug))).one()


async def _installations(db: AsyncSession) -> list[GitHubInstallation]:
    return list(await db.scalars(select(GitHubInstallation)))


# ------------------------------------------------------------------ start


async def test_install_redirects_admins_to_github_with_single_use_state(
    github_app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_redis: FakeAsyncRedis,
) -> None:
    user = await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    stored = cast("str | None", await fake_redis.get(f"github:install:state:{state}"))
    assert stored is not None
    assert str(user.id) in stored
    assert 0 < await fake_redis.ttl(f"github:install:state:{state}") <= 1800


async def test_members_cannot_start_an_install(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_with_org(github_app, client, db_session)
    member = await make_user(db_session)
    org_id = await _org_id(db_session, "acme")
    db_session.add(Membership(org_id=org_id, user_id=member.id, role=Role.MEMBER))
    await db_session.flush()
    await sign_in(github_app, client, db_session, member)
    response = await client.get(f"{API}/github/install", params={"org": "acme"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


async def test_outsiders_get_not_found(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_with_org(github_app, client, db_session)
    await sign_in(github_app, client, db_session)
    response = await client.get(f"{API}/github/install", params={"org": "acme"})
    assert response.status_code == 404


async def test_signed_out_install_goes_to_login(client: httpx.AsyncClient, web: str) -> None:
    response = await client.get(f"{API}/github/install", params={"org": "acme"})
    assert response.status_code == 302
    assert response.headers["location"].startswith(f"{web}/login?next=")


async def test_unconfigured_app_explains_itself(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, web: str
) -> None:
    await _admin_with_org(github_app, client, db_session)
    github_app.state.settings = github_app.state.settings.model_copy(
        update={"github": GitHubSettings()}
    )
    response = await client.get(f"{API}/github/install", params={"org": "acme"})
    assert response.headers["location"] == (
        f"{web}/acme/repos/new?github_error=github_app_not_configured"
    )


# ------------------------------------------------------------------ setup


async def test_setup_links_the_installation(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, web: str
) -> None:
    user = await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    with respx.mock(assert_all_called=False) as router:
        listing, details = _github(router)
        response = await _setup(client, state)
    assert response.status_code == 302
    assert response.headers["location"] == f"{web}/acme/repos/new?installed=1"
    # The user's own token proved access; the app's JWT read the details.
    assert listing.calls.last.request.headers["authorization"] == f"Bearer ghu_{user.login}"
    assert details.calls.last.request.headers["authorization"].startswith("Bearer ey")

    [row] = await _installations(db_session)
    assert (row.installation_id, row.account_login, row.account_type, row.account_id) == (
        INSTALLATION,
        "acme-gh",
        "Organization",
        5150,
    )
    assert (row.repository_selection, row.events, row.linked_by) == (
        "selected",
        ["push", "pull_request"],
        user.id,
    )
    assert row.permissions == {"contents": "read", "metadata": "read"}

    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "github.install.linked")
    )
    assert event is not None
    assert (event.actor_user_id, event.target_id) == (user.id, str(row.id))
    assert event.meta["installation_id"] == INSTALLATION
    assert event.meta["created"] is True

    listed = (await client.get(f"{API}/orgs/acme/github/installations")).json()
    assert [(i["account_login"], i["suspended"]) for i in listed] == [("acme-gh", False)]
    assert listed[0]["manage_url"] == (
        f"https://github.com/organizations/acme-gh/settings/installations/{INSTALLATION}"
    )


async def test_setup_from_onboarding_continues_onboarding(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, web: str
) -> None:
    await _admin_with_org(github_app, client, db_session)
    state = await _start(client, source="onboarding")
    with respx.mock(assert_all_called=False) as router:
        _github(router)
        response = await _setup(client, state)
    assert response.headers["location"] == f"{web}/onboarding?step=3&org=acme"


async def test_forged_state_is_rejected(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_with_org(github_app, client, db_session)
    with respx.mock(assert_all_called=False) as router:
        listing, _ = _github(router)
        response = await _setup(client, "forged-state")
    assert _error(response) == "invalid_state"
    assert not listing.called
    assert await _installations(db_session) == []


async def test_state_is_single_use(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    with respx.mock(assert_all_called=False) as router:
        _github(router)
        assert _error(await _setup(client, state)) is None
        assert _error(await _setup(client, state)) == "invalid_state"


async def test_state_started_by_someone_else_is_rejected(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """An attacker's install link must not attach the victim's installation to the attacker's
    org."""
    await _admin_with_org(github_app, client, db_session)
    attacker_state = await _start(client)
    victim = await make_user(db_session)
    await sign_in(github_app, client, db_session, victim)
    with respx.mock(assert_all_called=False) as router:
        listing, _ = _github(router)
        response = await _setup(client, attacker_state)
    assert _error(response) == "state_mismatch"
    assert not listing.called
    await set_org_context(db_session, await _org_id(db_session, "acme"))
    assert await _installations(db_session) == []


async def test_spoofed_installation_id_is_rejected(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """GitHub warns the Setup URL's installation_id can be forged: the user's token must list it."""
    await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    with respx.mock(assert_all_called=False) as router:
        _, details = _github(router, user_sees=[1, 2, 3])
        response = await _setup(client, state)
    assert _error(response) == "installation_not_accessible"
    assert not details.called
    assert await _installations(db_session) == []


@pytest.mark.parametrize(
    "membership",
    [
        # A plain member, or an outside collaborator on one repository (not a member at all),
        # sees the installation but must not hand all of its repositories to their own org.
        httpx.Response(200, json={"state": "active", "role": "member"}),
        httpx.Response(200, json={"state": "pending", "role": "admin"}),
        httpx.Response(200, json={"state": "active", "role": "billing_manager"}),
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(403, json={"message": "blocked"}),
    ],
)
async def test_seeing_an_org_installation_is_not_owning_it(
    membership: httpx.Response,
    github_app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    with respx.mock(assert_all_called=False) as router:
        _github(router, membership=membership)
        response = await _setup(client, state)
    assert _error(response) == "installation_not_owned"
    assert await _installations(db_session) == []


async def test_a_personal_installation_must_be_the_users_own(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, web: str
) -> None:
    user = await _admin_with_org(github_app, client, db_session)
    personal = {
        **INSTALLATION_BODY,
        "target_type": "User",
        "account": {"login": "someone-else", "id": 999, "type": "User"},
    }
    with respx.mock(assert_all_called=False) as router:
        _github(router)
        router.get(f"{GITHUB_API}/app/installations/{INSTALLATION}").mock(
            return_value=httpx.Response(200, json=personal)
        )
        refused = await _setup(client, await _start(client))
        mine = {**personal, "account": {"login": user.login, "id": user.github_id, "type": "User"}}
        router.get(f"{GITHUB_API}/app/installations/{INSTALLATION}").mock(
            return_value=httpx.Response(200, json=mine)
        )
        accepted = await _setup(client, await _start(client))
    assert _error(refused) == "installation_not_owned"
    assert accepted.headers["location"] == f"{web}/acme/repos/new?installed=1"
    [row] = await _installations(db_session)
    assert (row.account_type, row.account_login) == ("User", user.login)


async def test_enterprise_installations_are_refused(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_with_org(github_app, client, db_session)
    enterprise = {
        **INSTALLATION_BODY,
        "target_type": "Enterprise",
        "account": {"slug": "big-co", "id": 7, "name": "Big Co"},
    }
    with respx.mock(assert_all_called=False) as router:
        _github(router)
        router.get(f"{GITHUB_API}/app/installations/{INSTALLATION}").mock(
            return_value=httpx.Response(200, json=enterprise)
        )
        response = await _setup(client, await _start(client))
    assert _error(response) == "installation_unsupported"


async def test_someone_elses_attempt_does_not_use_up_the_state(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    await sign_in(github_app, client, db_session)  # another account presents it
    with respx.mock(assert_all_called=False) as router:
        _github(router)
        assert _error(await _setup(client, state)) == "state_mismatch"
        await sign_in(github_app, client, db_session, admin)
        assert _error(await _setup(client, state)) is None  # the admin can still finish


async def test_github_unreachable_is_reported(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    github_app.state.settings = github_app.state.settings.model_copy(
        update={"github": GH_SETTINGS.model_copy(update={"max_retries": 0})}
    )
    await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GITHUB_API}/user/installations").mock(
            side_effect=httpx.ConnectError("unreachable")
        )
        response = await _setup(client, state)
    assert _error(response) == "github_unavailable"


async def test_admins_demoted_mid_install_cannot_link(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    await db_session.execute(
        update(Membership).where(Membership.user_id == user.id).values(role=Role.MEMBER)
    )
    with respx.mock(assert_all_called=False) as router:
        listing, _ = _github(router)
        response = await _setup(client, state)
    assert _error(response) == "insufficient_role"
    assert not listing.called
    assert await _installations(db_session) == []


async def test_an_installation_belongs_to_one_org(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await _admin_with_org(github_app, client, db_session, "first-org")
    response = await client.post(
        f"{API}/orgs",
        json={"name": "Second", "slug": "second-org"},
        headers={"Origin": github_app.state.settings.web.origin, "X-CSRF-Token": _csrf(client)},
    )
    assert response.status_code == 201
    with respx.mock(assert_all_called=False) as router:
        _github(router)
        assert _error(await _setup(client, await _start(client, "first-org"))) is None
        response = await _setup(client, await _start(client, "second-org"))
    assert _error(response) == "installation_linked_elsewhere"
    # Still linked to the first org only, and the second org can't see that row.
    await set_org_context(db_session, await _org_id(db_session, "second-org"))
    assert await _installations(db_session) == []
    await set_org_context(db_session, await _org_id(db_session, "first-org"))
    [row] = await _installations(db_session)
    assert row.linked_by == user.id


async def test_relinking_refreshes_instead_of_duplicating(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_with_org(github_app, client, db_session)
    with respx.mock(assert_all_called=False) as router:
        _github(router)
        await _setup(client, await _start(client))
        router.get(f"{GITHUB_API}/app/installations/{INSTALLATION}").mock(
            return_value=httpx.Response(
                200, json={**INSTALLATION_BODY, "repository_selection": "all"}
            )
        )
        response = await _setup(client, await _start(client), setup_action="update")
    assert _error(response) is None
    [row] = await _installations(db_session)
    assert row.repository_selection == "all"
    actions = (
        await db_session.scalars(
            select(AuditEvent.meta).where(AuditEvent.action == "github.install.linked")
        )
    ).all()
    assert [meta["created"] for meta in actions] == [True, False]


async def test_github_outage_is_reported(
    github_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    settings = github_app.state.settings
    github_app.state.settings = settings.model_copy(
        update={"github": GH_SETTINGS.model_copy(update={"max_retries": 0})}
    )
    await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GITHUB_API}/user/installations").mock(return_value=httpx.Response(503))
        response = await _setup(client, state)
    assert _error(response) == "github_unavailable"


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"setup_action": "request"}, "install_requested"),
        ({}, "install_missing"),
    ],
)
async def test_setup_without_an_installation(
    params: dict[str, str],
    error: str,
    github_app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _admin_with_org(github_app, client, db_session)
    state = await _start(client)
    response = await client.get(f"{API}/github/setup", params={"state": state, **params})
    assert _error(response) == error


async def test_setup_without_state(
    client: httpx.AsyncClient, github_app: FastAPI, db_session: AsyncSession, web: str
) -> None:
    await _admin_with_org(github_app, client, db_session)
    plain = await client.get(f"{API}/github/setup", params={"installation_id": INSTALLATION})
    assert _error(plain) == "install_state_missing"
    updated = await client.get(
        f"{API}/github/setup", params={"installation_id": INSTALLATION, "setup_action": "update"}
    )
    assert updated.headers["location"] == f"{web}/"
    assert await _installations(db_session) == []


async def test_signed_out_setup_goes_to_login(client: httpx.AsyncClient, web: str) -> None:
    response = await client.get(f"{API}/github/setup", params={"installation_id": 1, "state": "x"})
    assert response.headers["location"] == f"{web}/login"
