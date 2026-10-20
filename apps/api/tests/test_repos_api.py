"""Repositories over HTTP: listing, connecting, importing, settings, disconnecting, and
permission mirroring, against a mocked GitHub."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import orjson
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api import streams
from sensei_core.auth.github_login import store_tokens
from sensei_core.config import GitHubSettings, Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.client import Credential, GitHubClient
from sensei_core.github.oauth import GitHubTokens
from sensei_core.indexing.events import JobOutbox
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AuditEvent,
    GitHubInstallation,
    IndexJob,
    Membership,
    Org,
    Repo,
    RepoSource,
    RepoStatus,
    Role,
    Stage,
    User,
)
from sensei_core.orgs.service import OrgService
from sensei_core.repos.access import RepoAccess
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
GITHUB_API = "https://api.github.com"
INSTALLATION = 555_001

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


def _repo(
    repo_id: int, full_name: str, *, private: bool = True, size: int = 2048
) -> dict[str, Any]:
    owner, name = full_name.split("/")
    return {
        "id": repo_id,
        "name": name,
        "full_name": full_name,
        "owner": {"login": owner},
        "private": private,
        "visibility": "private" if private else "public",
        "default_branch": "main",
        "size": size,
        "description": f"{name} description",
        "language": "Python",
        "pushed_at": "2026-10-18T10:00:00Z",
    }


PRIVATE_API = _repo(101, "acme-gh/api")
PRIVATE_WEB = _repo(102, "acme-gh/web")
SETTINGS_NAMED = _repo(103, "acme-gh/settings")
OTHER_API = _repo(104, "other-gh/api")
PUBLIC = _repo(201, "langflow-ai/langflow", private=False, size=150_000)
INSTALLED = [PRIVATE_API, PRIVATE_WEB, SETTINGS_NAMED, OTHER_API]


class FakeGitHub:
    """GitHub as seen by the installation and by each user's own token."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.user_can_read: dict[str, set[int]] = {}  # user token -> readable repository ids
        self.user_checks = 0
        self.public_lookups: list[str] = []
        router.post(f"{GITHUB_API}/app/installations/{INSTALLATION}/access_tokens").mock(
            return_value=httpx.Response(
                201,
                json={
                    "token": "ghs_installation",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "permissions": {"contents": "read"},
                },
            )
        )
        router.get(f"{GITHUB_API}/installation/repositories").mock(
            return_value=httpx.Response(
                200, json={"total_count": len(INSTALLED), "repositories": INSTALLED}
            )
        )
        router.get(f"{GITHUB_API}/user/installations/{INSTALLATION}/repositories").mock(
            side_effect=self._user_installation
        )
        router.get(url__regex=rf"{GITHUB_API}/repositories/\d+$").mock(side_effect=self._by_id)
        router.get(url__regex=rf"{GITHUB_API}/repos/[^/]+/[^/]+/languages$").mock(
            return_value=httpx.Response(200, json={"Python": 9000, "TypeScript": 1000})
        )
        router.get(f"{GITHUB_API}/repos/langflow-ai/langflow").mock(
            side_effect=self._anonymous(PUBLIC)
        )
        router.get(f"{GITHUB_API}/repos/acme-gh/api").mock(side_effect=self._anonymous(None))

    def _by_id(self, request: httpx.Request) -> httpx.Response:
        repo_id = int(request.url.path.rsplit("/", 1)[1])
        repo = next((r for r in INSTALLED if r["id"] == repo_id), None)
        auth = request.headers.get("authorization", "")
        if auth == "Bearer ghs_installation":
            return httpx.Response(200, json=repo) if repo else httpx.Response(404)
        self.user_checks += 1
        token = auth.removeprefix("Bearer ")
        if repo and repo_id in self.user_can_read.get(token, set()):
            return httpx.Response(200, json=repo)
        return httpx.Response(404, json={"message": "Not Found"})

    def _user_installation(self, request: httpx.Request) -> httpx.Response:
        """What the user's own token can see of the installation."""
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        assert token.startswith("ghu_")  # never the installation's token
        readable = [r for r in INSTALLED if r["id"] in self.user_can_read.get(token, set())]
        return httpx.Response(200, json={"total_count": len(readable), "repositories": readable})

    def _anonymous(self, repo: dict[str, Any] | None):
        def answer(request: httpx.Request) -> httpx.Response:
            # Public data, asked with the admin's own token (their rate limit, not the server's).
            self.public_lookups.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json=repo) if repo else httpx.Response(404)

        return answer


@pytest.fixture
def app(
    api_app: FastAPI,
    migrated_test_db: Settings,
    fake_redis: FakeAsyncRedis,
    db_session: AsyncSession,
) -> FastAPI:
    api_app.state.settings = migrated_test_db.model_copy(update={"github": GH_SETTINGS})
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    api_app.state.repo_jobs = PurgeLog()

    @asynccontextmanager
    async def same_session() -> AsyncGenerator[AsyncSession]:
        yield db_session  # streams' own sessions see the test's (rolled back) data

    api_app.state.stream_sessions = same_session
    return api_app


class PurgeLog:
    """The background work the API asked for (nothing is sent anywhere)."""

    def __init__(self) -> None:
        self.purged: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.indexed: list[uuid.UUID] = []
        self.revoked: list[str] = []

    async def purge(self, org_id: uuid.UUID, repo_id: uuid.UUID) -> None:
        self.purged.append((org_id, repo_id))

    async def index(self, org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID) -> None:
        self.indexed.append(job_id)

    async def revoke(self, task_ids: list[str]) -> None:
        self.revoked.extend(task_ids)


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        app.state.http = http
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test:
            yield test


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def fake(github: respx.MockRouter) -> FakeGitHub:
    return FakeGitHub(github)


async def _give_token(app: FastAPI, db: AsyncSession, user: User, token: str) -> None:
    await store_tokens(
        db,
        app.state.token_cipher,
        user,
        GitHubTokens(
            access_token=token,
            refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(hours=8),
            refresh_expires_at=None,
            scopes=[],
        ),
    )


def _readers(redis: FakeAsyncRedis) -> RepoAccess:
    """Only for dropping cached answers (it never asks GitHub here)."""

    async def no_token(_: User) -> str:
        raise AssertionError

    return RepoAccess(redis, GitHubClient(GH_SETTINGS, httpx.AsyncClient()), no_token)


async def _org_id(db: AsyncSession, slug: str) -> uuid.UUID:
    return (await db.scalars(select(Org.id).where(Org.slug == slug))).one()


async def _admin_org(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, slug: str = "acme"
) -> tuple[User, dict[str, str]]:
    user, headers = await sign_in(app, client, db)
    created = await client.post(
        f"{API}/orgs", json={"name": slug.title(), "slug": slug}, headers=headers
    )
    assert created.status_code == 201, created.text
    await _give_token(app, db, user, f"ghu_{user.login}")
    org_id = await _org_id(db, slug)
    await set_org_context(db, org_id)
    db.add(
        GitHubInstallation(
            org_id=org_id,
            installation_id=INSTALLATION if slug == "acme" else INSTALLATION + 1,
            account_login="acme-gh",
            account_type="Organization",
            account_id=1,
            repository_selection="selected",
        )
    )
    await db.flush()
    return user, headers


async def _member(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, role: Role = Role.MEMBER
) -> tuple[User, dict[str, str]]:
    member = await make_user(db)
    db.add(Membership(org_id=await _org_id(db, "acme"), user_id=member.id, role=role))
    await db.flush()
    await _give_token(app, db, member, f"ghu_{member.login}")
    return await sign_in(app, client, db, member)


# ------------------------------------------------------------------ available repositories


async def test_admins_see_what_the_installations_offer(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101, 102, 103, 104}
    page = (await client.get(f"{API}/orgs/acme/github/repos")).json()
    assert page["total"] == 4
    assert [r["full_name"] for r in page["items"]] == [
        "acme-gh/api",
        "acme-gh/settings",
        "acme-gh/web",
        "other-gh/api",
    ]
    assert page["items"][0]["account_login"] == "acme-gh"

    await client.post(f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers)
    filtered = (await client.get(f"{API}/orgs/acme/github/repos", params={"q": "API"})).json()
    assert [(r["full_name"], r["connected"]) for r in filtered["items"]] == [
        ("acme-gh/api", True),
        ("other-gh/api", False),
    ]
    paged = (
        await client.get(f"{API}/orgs/acme/github/repos", params={"page": 2, "per_page": 3})
    ).json()
    assert [r["full_name"] for r in paged["items"]] == ["other-gh/api"]


async def test_admins_only_get_what_they_can_read_on_github(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101, 102}
    page = (await client.get(f"{API}/orgs/acme/github/repos")).json()
    assert [r["full_name"] for r in page["items"]] == ["acme-gh/api", "acme-gh/web"]
    # The installation covers 103, but this admin can't read it on GitHub.
    for call in (
        client.post(f"{API}/orgs/acme/repos", json={"github_repo_id": 103}, headers=headers),
        client.post(
            f"{API}/orgs/acme/repos/estimate", json={"github_repo_id": 103}, headers=headers
        ),
    ):
        response = await call
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "repo_not_in_installation"


async def test_members_cannot_list_or_connect(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    await _admin_org(app, client, db_session)
    _, headers = await _member(app, client, db_session)
    assert (await client.get(f"{API}/orgs/acme/github/repos")).status_code == 403
    connect = await client.post(
        f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers
    )
    assert connect.status_code == 403
    imported = await client.post(
        f"{API}/orgs/acme/repos/import",
        json={"url": "https://github.com/langflow-ai/langflow"},
        headers=headers,
    )
    assert imported.status_code == 403


# ------------------------------------------------------------------ connect and import


async def test_connect_a_repository(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101, 102, 103, 104}
    response = await client.post(
        f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["slug"], body["full_name"], body["source"], body["status"]) == (
        "api",
        "acme-gh/api",
        "github_app",
        "pending",
    )
    assert (body["visibility"], body["size_kb"], body["default_branch"]) == (
        "private",
        2048,
        "main",
    )
    assert body["languages"] == {"Python": 9000, "TypeScript": 1000}
    assert body["settings"] == {"tracked_branches": [], "ignore_globs": [], "auto_index": True}

    row = await db_session.scalar(select(Repo).where(Repo.github_repo_id == 101))
    assert row is not None
    assert (row.connected_by, row.installation_id is not None) == (admin.id, True)
    event = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "repo.connected"))
    assert event is not None
    assert event.meta["full_name"] == "acme-gh/api"

    again = await client.post(
        f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers
    )
    assert (again.status_code, again.json()["error"]["code"]) == (409, "repo_already_connected")


async def test_a_repository_outside_the_installations_is_not_found(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    _, headers = await _admin_org(app, client, db_session)
    response = await client.post(
        f"{API}/orgs/acme/repos", json={"github_repo_id": 999}, headers=headers
    )
    assert (response.status_code, response.json()["error"]["code"]) == (
        404,
        "repo_not_in_installation",
    )


async def test_slugs_avoid_collisions_and_org_pages(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101, 102, 103, 104}
    slugs: list[str] = []
    for repo_id in (101, 104, 103):
        response = await client.post(
            f"{API}/orgs/acme/repos", json={"github_repo_id": repo_id}, headers=headers
        )
        slugs.append(response.json()["slug"])
    # Two "api" repositories; and "settings" would shadow /acme/settings.
    assert slugs == ["api", "other-gh-api", "acme-gh-settings"]


async def test_import_a_public_repository(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    _, headers = await _admin_org(app, client, db_session)
    response = await client.post(
        f"{API}/orgs/acme/repos/import",
        json={"url": "https://github.com/langflow-ai/langflow/tree/main"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["source"], body["visibility"], body["slug"]) == (
        "public_url",
        "public",
        "langflow",
    )
    row = await db_session.scalar(select(Repo).where(Repo.github_repo_id == 201))
    assert row is not None
    assert row.installation_id is None


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://github.com/acme-gh/api", "repo_not_public"),  # private: 404 to anonymous
        ("https://gitlab.com/a/b", "invalid_repo_url"),
        ("not a url", "invalid_repo_url"),
    ],
)
async def test_imports_that_are_refused(
    url: str,
    code: str,
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
) -> None:
    _, headers = await _admin_org(app, client, db_session)
    response = await client.post(
        f"{API}/orgs/acme/repos/import", json={"url": url}, headers=headers
    )
    assert (response.status_code, response.json()["error"]["code"]) == (422, code)


async def test_estimate_before_connecting(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101}
    by_url = await client.post(
        f"{API}/orgs/acme/repos/estimate",
        json={"url": "github.com/langflow-ai/langflow"},
        headers=headers,
    )
    assert by_url.status_code == 200, by_url.text
    estimate = by_url.json()
    assert estimate["full_name"] == "langflow-ai/langflow"
    assert fake.public_lookups == [f"Bearer ghu_{admin.login}"]  # not anonymous
    # The mocked GitHub reports 10 KB of code in two languages.
    assert (estimate["files"], estimate["tokens"]) == (1, 2857)  # 10,000 bytes / 3.5
    assert estimate["languages"] == {"Python": 9000, "TypeScript": 1000}
    assert estimate["index_minutes"] > 0
    assert float(estimate["cost_usd"]) > 0
    assert estimate["heuristic"] is True

    by_id = await client.post(
        f"{API}/orgs/acme/repos/estimate", json={"github_repo_id": 101}, headers=headers
    )
    assert (by_id.status_code, by_id.json()["private"]) == (200, True)
    for body in ({}, {"url": "github.com/a/b", "github_repo_id": 101}):
        bad = await client.post(f"{API}/orgs/acme/repos/estimate", json=body, headers=headers)
        assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_input")


# ------------------------------------------------------------------ permission mirroring


async def _connect_all(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    for repo_id in (101, 102):
        assert (
            await client.post(
                f"{API}/orgs/acme/repos", json={"github_repo_id": repo_id}, headers=headers
            )
        ).status_code == 201
    imported = await client.post(
        f"{API}/orgs/acme/repos/import",
        json={"url": "https://github.com/langflow-ai/langflow"},
        headers=headers,
    )
    assert imported.status_code == 201


async def test_members_see_only_what_github_lets_them_read(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101, 102}
    await _connect_all(client, headers)
    listed = [r["slug"] for r in (await client.get(f"{API}/orgs/acme/repos")).json()]
    assert listed == ["api", "web", "langflow"]

    member, _ = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = {102}  # no access to acme-gh/api on GitHub
    listed = [r["slug"] for r in (await client.get(f"{API}/orgs/acme/repos")).json()]
    assert listed == ["web", "langflow"]  # public imports are open to every member
    hidden = await client.get(f"{API}/orgs/acme/repos/api")
    assert (hidden.status_code, hidden.json()["error"]["code"]) == (
        404,
        "repo_not_found_or_no_access",
    )
    missing = await client.get(f"{API}/orgs/acme/repos/does-not-exist")
    assert missing.json()["error"]["code"] == "repo_not_found_or_no_access"  # indistinguishable
    assert (await client.get(f"{API}/orgs/acme/repos/web")).status_code == 200


async def test_access_answers_are_cached_and_can_be_dropped(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    fake_redis: FakeAsyncRedis,
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101}
    await client.post(f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers)
    assert fake.user_checks == 1  # connecting checks the admin can read it
    for _ in range(3):
        assert (await client.get(f"{API}/orgs/acme/repos/api")).status_code == 200
    assert fake.user_checks == 2  # one GitHub call, then the cache
    repo = await db_session.scalar(select(Repo).where(Repo.github_repo_id == 101))
    assert repo is not None
    key = f"access:{admin.id}:{repo.id}:0.0"
    assert 0 < await fake_redis.ttl(key) <= 300

    # Access revoked on GitHub: still allowed until the cache is dropped, then refused.
    fake.user_can_read[f"ghu_{admin.login}"] = set()
    assert (await client.get(f"{API}/orgs/acme/repos/api")).status_code == 200
    readers = _readers(fake_redis)
    await readers.forget_repo(repo.id)
    assert (await client.get(f"{API}/orgs/acme/repos/api")).status_code == 404
    assert fake.user_checks == 3
    assert await fake_redis.exists(f"access:{admin.id}:{repo.id}:0.1") == 1

    # Dropping by user works the same way.
    fake.user_can_read[f"ghu_{admin.login}"] = {101}
    await readers.forget_user(admin.id)
    assert (await client.get(f"{API}/orgs/acme/repos/api")).status_code == 200
    assert fake.user_checks == 4


async def test_an_answer_from_before_a_forget_is_never_read(
    db_session: AsyncSession, fake: FakeGitHub, fake_redis: FakeAsyncRedis
) -> None:
    """GitHub answers, a forget lands, then the (stale) answer is written: it must not count."""
    user = await make_user(db_session)

    async def token_for(_: User) -> str:
        return f"ghu_{user.login}"

    async with httpx.AsyncClient() as http:
        readers = RepoAccess(fake_redis, GitHubClient(GH_SETTINGS, http), token_for)
        await _forget_race(readers, user, fake)


async def _forget_race(readers: RepoAccess, user: User, fake: FakeGitHub) -> None:
    repo = Repo(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        source=RepoSource.GITHUB_APP,
        status=RepoStatus.READY,
        github_repo_id=101,
    )
    token = f"ghu_{user.login}"
    fake.user_can_read[token] = {101}
    original = readers._ask_github  # pyright: ignore[reportPrivateUsage]

    async def slow_answer(credential: Credential, asked: Repo) -> bool:
        allowed = await original(credential, asked)
        await readers.forget_repo(asked.id)  # revoked while GitHub was answering
        fake.user_can_read[token] = set()
        return allowed

    readers._ask_github = slow_answer  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    assert await readers.can_read(user, repo) is True  # the answer GitHub gave at the time
    readers._ask_github = original  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    assert await readers.can_read(user, repo) is False  # asked again, not the stale answer


async def test_without_a_github_token_members_are_asked_to_sign_in_again(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101}
    await client.post(f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers)
    stranger = await make_user(db_session)  # a member who never stored a GitHub token
    db_session.add(
        Membership(org_id=await _org_id(db_session, "acme"), user_id=stranger.id, role=Role.MEMBER)
    )
    await db_session.flush()
    await sign_in(app, client, db_session, stranger)
    response = await client.get(f"{API}/orgs/acme/repos")
    assert (response.status_code, response.json()["error"]["code"]) == (
        401,
        "github_reauth_required",
    )


async def test_other_orgs_never_see_the_repository(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101}
    await client.post(f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers)
    await _admin_org(app, client, db_session, "rival")
    assert (await client.get(f"{API}/orgs/rival/repos")).json() == []
    assert (await client.get(f"{API}/orgs/rival/repos/api")).status_code == 404
    # Row-level security: in rival's context the row doesn't exist at all.
    await set_org_context(db_session, await _org_id(db_session, "rival"))
    assert (await db_session.scalars(select(Repo))).all() == []


# ------------------------------------------------------------------ settings and disconnect


async def test_admins_change_settings(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101}
    await client.post(f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers)
    response = await client.patch(
        f"{API}/orgs/acme/repos/api",
        json={"tracked_branches": ["main", "release/2.x"], "auto_index": False},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"] == {
        "tracked_branches": ["main", "release/2.x"],
        "ignore_globs": [],
        "auto_index": False,
    }
    partial = await client.patch(
        f"{API}/orgs/acme/repos/api", json={"ignore_globs": ["dist/**"]}, headers=headers
    )
    assert partial.json()["settings"]["tracked_branches"] == ["main", "release/2.x"]
    bad = await client.patch(
        f"{API}/orgs/acme/repos/api", json={"tracked_branches": ["no spaces"]}, headers=headers
    )
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_settings")
    assert "not a valid branch name" in bad.json()["error"]["details"]["problems"][0]

    member, member_headers = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = {101}
    forbidden = await client.patch(
        f"{API}/orgs/acme/repos/api", json={"auto_index": True}, headers=member_headers
    )
    assert forbidden.status_code == 403


async def test_disconnect_hides_then_purges(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, headers = await _admin_org(app, client, db_session)
    fake.user_can_read[f"ghu_{admin.login}"] = {101}
    created = await client.post(
        f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers
    )
    repo_id = uuid.UUID(created.json()["id"])
    assert (await client.get(f"{API}/orgs/acme/repos/api")).status_code == 200  # cached access
    response = await client.delete(f"{API}/orgs/acme/repos/api", headers=headers)
    assert response.status_code == 204
    # Its cached access answer is gone with it.
    assert await app.state.redis.exists(f"access:{admin.id}:{repo_id}") == 0
    org = await db_session.scalar(select(Org).where(Org.slug == "acme"))
    assert org is not None
    assert app.state.repo_jobs.purged == [(org.id, repo_id)]
    assert (await client.get(f"{API}/orgs/acme/repos")).json() == []
    assert (await client.get(f"{API}/orgs/acme/repos/api")).status_code == 404
    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "repo.disconnected")
    )
    assert event is not None

    # Connecting it again before the purge makes a new repository: nothing of the old one
    # (snapshots, jobs, settings) comes back, and the old row still gets purged.
    again = await client.post(
        f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers
    )
    assert again.status_code == 201
    assert again.json()["id"] != str(repo_id)
    old = await db_session.get(Repo, repo_id)
    assert old is not None
    assert old.deleted_at is not None


# ------------------------------------------------------------------ index jobs


async def _connected(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, fake: FakeGitHub
) -> tuple[User, dict[str, str], str]:
    """An admin, and acme-gh/api connected (which queues its first index run)."""
    admin, headers = await _admin_org(app, client, db)
    fake.user_can_read[f"ghu_{admin.login}"] = {101, 102}
    created = await client.post(
        f"{API}/orgs/acme/repos", json={"github_repo_id": 101}, headers=headers
    )
    assert created.status_code == 201, created.text
    return admin, headers, created.json()["index_job_id"]


async def _stream_events(fake_redis: FakeAsyncRedis, key: str) -> list[dict[str, Any]]:
    entries = await fake_redis.xrange(key)
    return [orjson.loads(fields["data"]) for _, fields in entries]


def _sse(body: str) -> list[dict[str, str]]:
    """The events of an SSE body: field -> value per event (comments dropped)."""
    events: list[dict[str, str]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line and not line.startswith(":"):
                name, _, value = line.partition(":")
                fields[name] = value.lstrip(" ")
        if fields:
            events.append(fields)
    return events


async def _finish(db: AsyncSession, job_id: str, fake_redis: FakeAsyncRedis) -> None:
    """Run the job's state machine to the end and publish its events, as the workers would."""
    job = await db.get(IndexJob, uuid.UUID(job_id))
    assert job is not None
    machine = IndexJobStateMachine(job)
    machine.start()
    machine.update_progress(Stage.CLONE, 1, 2)
    machine.advance(Stage.DISCOVER)
    machine.log("Found 3 files to index.")
    machine.advance(Stage.FINALIZE)
    machine.finish()
    outbox = JobOutbox()
    outbox.collect(machine)
    await db.flush()
    await outbox.publish(fake_redis)


async def test_connecting_queues_the_first_index_run(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    fake_redis: FakeAsyncRedis,
) -> None:
    _, _, job_id = await _connected(app, client, db_session, fake)
    assert app.state.repo_jobs.indexed == [uuid.UUID(job_id)]  # sent after the commit
    history = (await client.get(f"{API}/orgs/acme/repos/api/index-jobs")).json()
    assert [(j["id"], j["status"], j["kind"]) for j in history] == [(job_id, "queued", "full")]
    detail = (await client.get(f"{API}/orgs/acme/repos/api/index-jobs/{job_id}")).json()
    assert (detail["progress"], detail["features"]) == (0.0, [])
    queued = await _stream_events(fake_redis, f"jobs:{job_id}")
    assert [e["type"] for e in queued] == ["queued"]
    org = await _org_id(db_session, "acme")
    assert [e["type"] for e in await _stream_events(fake_redis, f"org:{org}:events")] == ["queued"]


async def test_reindex_and_cancel(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    fake_redis: FakeAsyncRedis,
) -> None:
    _, headers, job_id = await _connected(app, client, db_session, fake)
    busy = await client.post(f"{API}/orgs/acme/repos/api/reindex", headers=headers)
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "index_job_running")

    await cast(
        "Awaitable[int]", fake_redis.rpush(f"index-job:{job_id}:tasks", "t-clone", "t-discover")
    )
    cancelled = await client.post(
        f"{API}/orgs/acme/repos/api/index-jobs/{job_id}/cancel", headers=headers
    )
    assert (cancelled.status_code, cancelled.json()["status"]) == (200, "cancelled")
    assert app.state.repo_jobs.revoked == ["t-clone", "t-discover"]
    assert (await _stream_events(fake_redis, f"jobs:{job_id}"))[-1]["type"] == "cancelled"
    again = await client.post(
        f"{API}/orgs/acme/repos/api/index-jobs/{job_id}/cancel", headers=headers
    )
    assert (again.status_code, again.json()["error"]["code"]) == (409, "index_job_finished")

    started = await client.post(f"{API}/orgs/acme/repos/api/reindex", headers=headers)
    assert started.status_code == 202
    assert started.json()["status"] == "queued"
    assert len(app.state.repo_jobs.indexed) == 2
    missing = await client.get(f"{API}/orgs/acme/repos/api/index-jobs/{uuid.uuid4()}")
    assert missing.json()["error"]["code"] == "index_job_not_found"


async def test_members_can_watch_but_not_start_or_cancel(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    _, _, job_id = await _connected(app, client, db_session, fake)
    member, member_headers = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = {101}
    assert (await client.get(f"{API}/orgs/acme/repos/api/index-jobs")).status_code == 200
    reindex = await client.post(f"{API}/orgs/acme/repos/api/reindex", headers=member_headers)
    cancel = await client.post(
        f"{API}/orgs/acme/repos/api/index-jobs/{job_id}/cancel", headers=member_headers
    )
    assert (reindex.status_code, cancel.status_code) == (403, 403)


async def test_progress_events_replay_and_resume(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    fake_redis: FakeAsyncRedis,
) -> None:
    _, _, job_id = await _connected(app, client, db_session, fake)
    await _finish(db_session, job_id, fake_redis)
    url = f"{API}/orgs/acme/repos/api/index-jobs/{job_id}/events"

    response = await client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    events = _sse(response.text)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "queued"
    assert kinds[-1] == "done"  # and the stream closed after it
    assert "unlock" in kinds
    assert "log" in kinds
    ids = [e["id"] for e in events]
    assert ids == sorted(ids, key=lambda i: tuple(map(int, i.split("-"))))
    assert orjson.loads(events[-1]["data"])["id"] == ids[-1]

    # Reconnecting with the last id seen: only what came after.
    resumed = _sse((await client.get(url, headers={"Last-Event-ID": ids[2]})).text)
    assert [e["id"] for e in resumed] == ids[3:]
    bad = await client.get(url, headers={"Last-Event-ID": "not-an-id"})
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_last_event_id")

    # A week later the stream is gone; the database still says how it ended.
    await fake_redis.delete(f"jobs:{job_id}")
    expired = _sse((await client.get(url)).text)
    assert [e["event"] for e in expired] == ["done"]


async def _drive(app: FastAPI, path: str, cookies: httpx.Cookies, *, seconds: float) -> str:
    """The first ``seconds`` of a streaming response (httpx's ASGI transport waits for the
    end, and these streams don't end)."""
    cookie = "; ".join(f"{name}={value}" for name, value in cookies.items())
    path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    body: list[bytes] = []
    disconnected = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if not body and not disconnected.is_set() and not hasattr(receive, "sent"):
            receive.sent = True  # type: ignore[attr-defined]
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            body.append(message.get("body", b""))

    task = asyncio.create_task(cast("Any", app)(scope, receive, send))
    await asyncio.sleep(seconds)
    disconnected.set()
    try:
        await asyncio.wait_for(task, timeout=5)
    except TimeoutError:
        task.cancel()
    return b"".join(body).decode()


async def test_a_quiet_stream_sends_heartbeats(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streams, "HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(streams, "READ_BLOCK_MS", 100)
    _, _, job_id = await _connected(app, client, db_session, fake)
    path = f"{API}/orgs/acme/repos/api/index-jobs/{job_id}/events"
    body = await _drive(app, path, client.cookies, seconds=1.0)
    assert [e["event"] for e in _sse(body)] == ["queued"]  # still queued: nothing else yet
    assert body.count(": ping") >= 2  # comment lines keep proxies from closing it


async def test_org_events_only_show_readable_repositories(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    fake_redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streams, "READ_BLOCK_MS", 100)
    _, headers, first = await _connected(app, client, db_session, fake)
    web = await client.post(f"{API}/orgs/acme/repos", json={"github_repo_id": 102}, headers=headers)
    second = web.json()["index_job_id"]
    # A member who can read acme-gh/web on GitHub but not acme-gh/api.
    member, _ = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = {102}
    body = await _drive(app, f"{API}/orgs/acme/events?last_event_id=0", client.cookies, seconds=1.0)
    seen = {orjson.loads(e["data"])["job_id"] for e in _sse(body)}
    assert seen == {second}
    assert first not in seen
