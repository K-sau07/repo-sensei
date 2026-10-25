"""Repositories over HTTP: listing, connecting, importing, settings, disconnecting, and
permission mirroring, against a mocked GitHub."""

from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from collections.abc import AsyncGenerator, Awaitable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import orjson
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI, Request
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api import streams
from sensei_api.browse import TreeCache
from sensei_api.routers import index_jobs
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
    JobKind,
    JobStatus,
    Membership,
    Org,
    Repo,
    RepoFile,
    RepoSource,
    RepoStatus,
    Role,
    Snapshot,
    SnapshotStatus,
    Stage,
    User,
    UserSession,
)
from sensei_core.orgs.service import OrgService
from sensei_core.repos.access import RepoAccess
from sensei_core.storage import Bucket, ObjectStorage, artifact_key, repo_prefix, snapshot_key
from sensei_engine.chunk.files import chunk_file
from sensei_engine.chunk.store import FileChunks, save_chunks
from sensei_engine.ingest.constellation import (
    LIVE_BATCH,
    Constellation,
    DirectoryLayout,
    LiveConstellation,
    artifact_parts,
    layer_for,
)
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.merkle import ARTIFACT_NAME, Leaf, build_tree
from sensei_engine.ingest.store import save_discovery
from sensei_engine.parse.parser import parse_file
from sensei_engine.search.grep import ExactSearch
from sensei_engine.search.route import Lane
from sensei_engine.search.service import SearchService
from sensei_engine.snapshots import SnapshotCache
from sensei_engine.symbols.extract import extract_symbols
from sensei_engine.symbols.store import FileSymbols, save_symbols
from sensei_testing import make_user, sign_in
from sensei_workers.ingest.archive import write_archive

pytestmark = pytest.mark.integration

API = "/api/v1"
LANES = [lane.value for lane in Lane]
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
PUBLIC = {
    **_repo(201, "langflow-ai/langflow", private=False, size=150_000),
    "stargazers_count": 120,
}
INSTALLED = [PRIVATE_API, PRIVATE_WEB, SETTINGS_NAMED, OTHER_API]


class FakeGitHub:
    """GitHub as seen by the installation and by each user's own token."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.user_can_read: dict[str, set[int]] = {}  # user token -> readable repository ids
        self.user_checks = 0
        self.public_lookups: list[str] = []
        self.tree_lookups: list[str] = []  # the credential each tree was asked with
        self.tree_status = 200
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
        router.get(url__regex=rf"{GITHUB_API}/repos/[^/]+/[^/]+/git/trees/main(\?.*)?$").mock(
            side_effect=self._tree
        )

    def _tree(self, request: httpx.Request) -> httpx.Response:
        self.tree_lookups.append(request.headers.get("authorization", ""))
        if self.tree_status != 200:
            return httpx.Response(self.tree_status, json={"message": "Git Repository is empty."})
        assert request.url.params["recursive"] == "1"
        blobs = [("app.py", 3_000), ("src/view.ts", 7_000), ("README.md", 500), ("logo.png", 9_000)]
        entries = [{"path": "src", "type": "tree"}] + [
            {"path": path, "type": "blob", "size": size} for path, size in blobs
        ]
        return httpx.Response(200, json={"sha": "t", "tree": entries, "truncated": False})

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
    assert again.json()["error"]["details"] == {"slug": "api"}  # so the UI can link to it


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
    # Files from the default branch's tree (4, two of them code: 10,000 bytes / 3.5 tokens).
    assert fake.tree_lookups == [f"Bearer ghu_{admin.login}"]
    assert (estimate["files"], estimate["tokens"]) == (4, 2857)
    assert estimate["languages"] == {"Python": 9000, "TypeScript": 1000}
    # No Voyage or Anthropic key in tests: embeddings and summaries won't run, so they add neither
    # time nor cost (four files take seconds: 0.0 minutes).
    assert estimate["index_minutes"] >= 0
    assert float(estimate["cost_usd"]) >= 0
    assert estimate["heuristic"] is True
    assert estimate["unpriced"] == []
    assert (estimate["size_kb"], estimate["stars"]) == (150_000, 120)

    by_id = await client.post(
        f"{API}/orgs/acme/repos/estimate", json={"github_repo_id": 101}, headers=headers
    )
    assert (by_id.status_code, by_id.json()["private"]) == (200, True)
    assert fake.tree_lookups[-1] == "Bearer ghs_installation"  # a private one, as the app
    # Without a tree (an empty repository, say) the language bytes are the basis.
    fake.tree_status = 409
    no_tree = await client.post(
        f"{API}/orgs/acme/repos/estimate",
        json={"url": "github.com/langflow-ai/langflow"},
        headers=headers,
    )
    assert no_tree.status_code == 200, no_tree.text
    # 10,000 bytes of code: 2 code files at 4 KB, 3 files in all at 70% code.
    assert (no_tree.json()["files"], no_tree.json()["tokens"]) == (3, 2857)
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


def test_a_stage_done_with_work_left_is_reported_partial() -> None:
    """Summaries cut short finish as done (the wiki works on what was written); the job says
    what the next index completes."""
    job = IndexJob(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        repo_id=uuid.uuid4(),
        kind=JobKind.FULL,
        status=JobStatus.SUCCEEDED,
        stage=Stage.FINALIZE,
        progress=1.0,
        stages={
            "chunks": {"status": "done", "details": {"chunks": 12}},
            "summaries": {"status": "done", "details": {"partial": {"directories": True}}},
            "embeddings": {"status": "skipped", "details": {"partial": {"files": 1}}},
        },
        created_at=datetime.now(UTC),
    )
    out = index_jobs.IndexJobOut.build(job)
    assert out.partial == {"summaries": index_jobs.PartialOut(directories=True)}
    assert "wiki" in out.features
    # A malformed entry is left out, not a 500.
    job.stages = {"summaries": {"status": "done", "details": {"partial": "oops"}}, "x": 3}
    assert index_jobs.IndexJobOut.build(job).partial == {}


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


async def test_a_run_whose_final_event_was_lost_still_ends_its_stream(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streams, "READ_BLOCK_MS", 50)
    monkeypatch.setattr(streams, "RECHECK_SECONDS", 0.1)
    _, _, job_id = await _connected(app, client, db_session, fake)
    # The run ends in the database, but its final event never reaches Redis (a blip after the
    # commit): the open stream finds out from the database and ends with the outcome.
    job = await db_session.get(IndexJob, uuid.UUID(job_id))
    assert job is not None
    machine = IndexJobStateMachine(job)
    machine.start()
    machine.advance(Stage.FINALIZE)
    machine.finish()
    await db_session.flush()
    url = f"{API}/orgs/acme/repos/api/index-jobs/{job_id}/events"
    events = _sse((await client.get(url)).text)
    assert [e["event"] for e in events] == ["queued", "done"]

    # Resuming after the last event seen, with the stream long gone, gets the outcome too.
    seen = events[0]["id"]
    resumed = _sse((await client.get(url, headers={"Last-Event-ID": seen})).text)
    assert [e["event"] for e in resumed] == ["done"]


async def test_an_expired_stream_resumed_mid_way_still_gets_the_outcome(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    fake_redis: FakeAsyncRedis,
) -> None:
    _, _, job_id = await _connected(app, client, db_session, fake)
    await _finish(db_session, job_id, fake_redis)
    await fake_redis.delete(f"jobs:{job_id}")
    url = f"{API}/orgs/acme/repos/api/index-jobs/{job_id}/events"
    resumed = _sse((await client.get(url, headers={"Last-Event-ID": "1-0"})).text)
    assert [e["event"] for e in resumed] == ["done"]


async def test_the_org_stream_ends_when_its_viewer_loses_access(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streams, "READ_BLOCK_MS", 50)
    monkeypatch.setattr(index_jobs, "MEMBERSHIP_RECHECK_SECONDS", 0.0)
    await _connected(app, client, db_session, fake)

    async def gone(*_args: object) -> bool:
        return False

    monkeypatch.setattr(index_jobs, "_still_member", gone)
    # The stream would replay the queued run; it ends instead, and sends nothing.
    response = await client.get(f"{API}/orgs/acme/events?last_event_id=0")
    assert response.status_code == 200
    assert _sse(response.text) == []


async def test_still_member_follows_the_session_and_the_membership(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeGitHub
) -> None:
    admin, _, _ = await _connected(app, client, db_session, fake)
    org_id = await _org_id(db_session, "acme")
    session = await db_session.scalar(
        select(UserSession)
        .where(UserSession.user_id == admin.id)
        .order_by(UserSession.created_at.desc())
    )
    assert session is not None
    request = Request({"type": "http", "app": app})

    async def check() -> bool:
        return await index_jobs._still_member(  # pyright: ignore[reportPrivateUsage]
            request, org_id, admin.id, session.id
        )

    assert await check()
    session.revoked_at = datetime.now(UTC)
    await db_session.flush()
    assert not await check()
    session.revoked_at = None
    membership = await db_session.get(Membership, (org_id, admin.id))
    assert membership is not None
    await db_session.delete(membership)
    await db_session.flush()
    assert not await check()


# ------------------------------------------------------------------ browsing a snapshot

SHA_ONE = "1" * 40
SHA_TWO = "2" * 40
BIG = 2 * 1024 * 1024 + 10


@pytest.fixture
async def storage(app: FastAPI, tmp_path: Path) -> AsyncGenerator[ObjectStorage]:
    store = ObjectStorage(Settings(env="test").s3)
    await store.start()
    app.state.storage = store
    app.state.snapshot_cache = SnapshotCache(
        tmp_path / "snapshots", max_bytes=10**9, max_snapshot_bytes=10**9
    )
    app.state.tree_cache = TreeCache()
    created: list[tuple[uuid.UUID, uuid.UUID]] = []
    app.state.created_repos = created
    yield store
    for org_id, repo_id in created:
        for bucket in Bucket:
            await store.delete_prefix(bucket, repo_prefix(bucket, org_id, repo_id))
    await store.close()


async def _index(
    app: FastAPI,
    db: AsyncSession,
    store: ObjectStorage,
    tmp_path: Path,
    files: dict[str, bytes],
    sha: str,
    *,
    outcome: JobStatus = JobStatus.SUCCEEDED,
    links: dict[str, Path] | None = None,
) -> None:
    """acme-gh/api indexed at ``sha`` with ``files``, the way the workers leave it (a run that
    ended with ``outcome``: only a succeeded one moves the repository's last index)."""
    repo = await db.scalar(select(Repo).where(Repo.slug == "api"))
    assert repo is not None
    app.state.created_repos.append((repo.org_id, repo.id))
    root = tmp_path / sha
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    for path, target in (links or {}).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).symlink_to(target, target_is_directory=target.is_dir())
    found = discover(root, max_files=1000)
    snapshot = Snapshot(
        org_id=repo.org_id,
        repo_id=repo.id,
        ref="main",
        commit_sha=sha,
        status=SnapshotStatus.READY,
        ready_at=datetime.now(UTC),
    )
    db.add(snapshot)
    await db.flush()
    db.add(
        IndexJob(
            org_id=repo.org_id,
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            kind=JobKind.FULL,
            status=outcome,
        )
    )
    await save_discovery(db, snapshot, found)
    tree = build_tree(Leaf(f.path, f.content_hash, f.fast_hash, f.size) for f in found.files)
    key = artifact_key(repo.org_id, repo.id, sha, ARTIFACT_NAME)
    await store.put_bytes(Bucket.ARTIFACTS, key, tree.to_bytes())
    archive = write_archive(root, tmp_path / f"{sha}.tar.zst")
    await store.put_file(Bucket.SNAPSHOTS, snapshot_key(repo.org_id, repo.id, sha), archive.path)
    snapshot.merkle_root = tree.root.hex()
    if outcome is JobStatus.SUCCEEDED:
        repo.last_indexed_sha = sha
    await db.flush()


FILES = {
    "README.md": b"# Demo\n",
    "src/app.py": b"def main():\n    return 1\n",
    "src/user_controller.py": b"class UserController: ...\n",
    "src/lib/util.py": b"X = 1\n",
    "docs/guide.md": b"Guide\n",
    "logo.png": b"\x89PNG\r\n\x1a\n\0\0",
    "Zeta.txt": b"z\n",
    "big.txt": b"y" * BIG,
}


async def test_tree_lists_directories_first_with_etags(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    root = await client.get(f"{API}/orgs/acme/repos/api/tree")
    assert root.status_code == 200, root.text
    body = root.json()
    assert root.headers["x-commit-sha"] == SHA_ONE
    assert "commit_sha" not in body  # the body depends only on the directory
    assert [(e["name"], e["type"]) for e in body["entries"]] == [
        ("docs", "dir"),
        ("src", "dir"),
        ("big.txt", "file"),
        ("logo.png", "file"),
        ("README.md", "file"),
        ("Zeta.txt", "file"),
    ]
    src = next(e for e in body["entries"] if e["name"] == "src")
    assert src["child_count"] == 3
    readme = next(e for e in body["entries"] if e["name"] == "README.md")
    assert (readme["language"], readme["role"], readme["loc"]) == ("markdown", "docs", 1)
    etag = root.headers["etag"]
    again = await client.get(f"{API}/orgs/acme/repos/api/tree", headers={"If-None-Match": etag})
    assert (again.status_code, again.headers["etag"]) == (304, etag)

    nested = (await client.get(f"{API}/orgs/acme/repos/api/tree", params={"path": "src/"})).json()
    assert [e["path"] for e in nested["entries"]] == [
        "src/lib",
        "src/app.py",
        "src/user_controller.py",
    ]
    not_dir = await client.get(f"{API}/orgs/acme/repos/api/tree", params={"path": "README.md"})
    assert not_dir.json()["error"]["code"] == "not_a_directory"
    missing = await client.get(f"{API}/orgs/acme/repos/api/tree", params={"path": "nope"})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "path_not_found")

    # A later commit that only touched src/: docs/ answers 304 to the old ETag, src/ doesn't.
    docs_etag = (
        await client.get(f"{API}/orgs/acme/repos/api/tree", params={"path": "docs"})
    ).headers["etag"]
    src_etag = (
        await client.get(f"{API}/orgs/acme/repos/api/tree", params={"path": "src"})
    ).headers["etag"]
    await _index(app, db_session, storage, tmp_path, {**FILES, "src/app.py": b"changed\n"}, SHA_TWO)
    for path, etag, code in (("docs", docs_etag, 304), ("src", src_etag, 200)):
        response = await client.get(
            f"{API}/orgs/acme/repos/api/tree",
            params={"path": path},
            headers={"If-None-Match": etag},
        )
        assert response.status_code == code, path
        # Even a 304 says which commit it's from now (it updates the cached headers).
        assert response.headers["x-commit-sha"] == SHA_TWO, path
    old = await client.get(f"{API}/orgs/acme/repos/api/tree", params={"ref": SHA_ONE[:8]})
    assert old.headers["x-commit-sha"] == SHA_ONE
    branch = await client.get(f"{API}/orgs/acme/repos/api/tree", params={"ref": "main"})
    assert branch.headers["x-commit-sha"] == SHA_TWO
    unknown = await client.get(f"{API}/orgs/acme/repos/api/tree", params={"ref": "nope"})
    assert unknown.json()["error"]["code"] == "ref_not_indexed"


async def test_a_first_run_is_browsable_once_discovery_is_done(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    _, _, job_id = await _connected(app, client, db_session, fake)
    tree = f"{API}/orgs/acme/repos/api/tree"
    early = await client.get(tree)
    assert (early.status_code, early.json()["error"]["code"]) == (404, "repo_not_indexed")
    # Discovery has written the rows; the Merkle stage hasn't run (the code browser unlocks
    # at discovery). A failed earlier run left the snapshot's archive, which reads the same.
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE, outcome=JobStatus.FAILED)
    snapshot = await db_session.scalar(select(Snapshot).where(Snapshot.commit_sha == SHA_ONE))
    job = await db_session.get(IndexJob, uuid.UUID(job_id))
    assert snapshot is not None
    assert job is not None
    snapshot.merkle_root = None
    job.status, job.snapshot_id = JobStatus.RUNNING, snapshot.id
    job.stages = {"discover": {"status": "done"}}
    await db_session.flush()

    root = await client.get(tree)
    assert root.status_code == 200, root.text
    assert (root.headers["x-commit-sha"], root.headers["cache-control"]) == (SHA_ONE, "no-store")
    assert "etag" not in root.headers
    assert [(e["name"], e["type"]) for e in root.json()["entries"]] == [
        ("docs", "dir"),
        ("src", "dir"),
        ("big.txt", "file"),
        ("logo.png", "file"),
        ("README.md", "file"),
        ("Zeta.txt", "file"),
    ]
    src = next(e for e in root.json()["entries"] if e["name"] == "src")
    assert src["child_count"] == 3
    nested = (await client.get(tree, params={"path": "src"})).json()
    assert [e["path"] for e in nested["entries"]] == [
        "src/lib",
        "src/app.py",
        "src/user_controller.py",
    ]
    not_dir = await client.get(tree, params={"path": "README.md"})
    assert not_dir.json()["error"]["code"] == "not_a_directory"
    missing = await client.get(tree, params={"path": "nope"})
    assert missing.json()["error"]["code"] == "path_not_found"
    shown = await client.get(f"{API}/orgs/acme/repos/api/file", params={"path": "src/app.py"})
    assert shown.json()["content"] == FILES["src/app.py"].decode()


async def test_a_snapshot_whose_run_failed_is_not_browsable(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    # A later run got as far as the tree, then failed: that commit was never indexed.
    await _index(app, db_session, storage, tmp_path, FILES, SHA_TWO, outcome=JobStatus.FAILED)
    tree = f"{API}/orgs/acme/repos/api/tree"
    branch = await client.get(tree, params={"ref": "main"})
    assert branch.headers["x-commit-sha"] == SHA_ONE
    commit = await client.get(tree, params={"ref": SHA_TWO[:8]})
    assert (commit.status_code, commit.json()["error"]["code"]) == (404, "ref_not_indexed")


async def test_symlinks_never_lead_outside_the_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours\n")
    _, headers, _ = await _connected(app, client, db_session, fake)
    await _index(
        app,
        db_session,
        storage,
        tmp_path,
        FILES,
        SHA_ONE,
        links={"src/secret.txt": outside / "secret.txt", "linked": outside},
    )
    url = f"{API}/orgs/acme/repos/api/file"
    for path in ("src/secret.txt", "linked/secret.txt"):
        response = await client.get(url, params={"path": path})
        assert response.status_code == 404, path
        assert "not yours" not in response.text
    listing = (await client.get(f"{API}/orgs/acme/repos/api/tree", params={"path": "src"})).json()
    assert "src/secret.txt" not in [e["path"] for e in listing["entries"]]
    found = await client.post(
        f"{API}/orgs/acme/repos/api/grep", json={"pattern": "not yours"}, headers=headers
    )
    assert (found.status_code, found.json()["hits"]) == (200, [])


async def test_files_text_binary_and_large(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    github: respx.MockRouter,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    url = f"{API}/orgs/acme/repos/api/file"
    response = await client.get(url, params={"path": "src/app.py"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["content"], body["encoding"], body["language"], body["loc"]) == (
        "def main():\n    return 1\n",
        "utf-8",
        "python",
        2,
    )
    content = FILES["src/app.py"]
    assert (
        body["sha"]
        == hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()
    )
    etag = response.headers["etag"]
    assert etag == f'"f1-{hashlib.sha256(content).hexdigest()}"'
    cached = await client.get(url, params={"path": "src/app.py"}, headers={"If-None-Match": etag})
    assert (cached.status_code, cached.headers["x-commit-sha"]) == (304, SHA_ONE)
    assert response.headers["x-commit-sha"] == SHA_ONE

    binary = await client.get(url, params={"path": "logo.png"})
    assert (binary.status_code, binary.json()["error"]["code"]) == (415, "binary_file")
    assert binary.json()["error"]["details"]["size"] == len(FILES["logo.png"])
    github.route(host="localhost", port=9000).pass_through()  # the local object storage
    async with httpx.AsyncClient() as http:
        saved = await http.get(binary.json()["error"]["details"]["download_url"])
    assert saved.content == FILES["logo.png"]  # binaries get a download link too

    large = await client.get(url, params={"path": "big.txt"})
    assert (large.status_code, large.json()["error"]["code"]) == (413, "file_too_large")
    link = large.json()["error"]["details"]["download_url"]
    async with httpx.AsyncClient() as http:
        downloaded = await http.get(link)
    assert downloaded.content == FILES["big.txt"]

    missing = await client.get(url, params={"path": "src/nope.py"})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "path_not_found")


@pytest.mark.parametrize(
    "path",
    ["../etc/passwd", "/etc/passwd", "src/../../etc/passwd", "src/./app.py", "src//app.py", "a\\b"],
)
async def test_path_traversal_is_refused(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
    path: str,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, {"src/app.py": b"x\n"}, SHA_ONE)
    for endpoint in ("file", "tree"):
        response = await client.get(f"{API}/orgs/acme/repos/api/{endpoint}", params={"path": path})
        assert (response.status_code, response.json()["error"]["code"]) == (400, "invalid_path")


async def test_fuzzy_path_search(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    url = f"{API}/orgs/acme/repos/api/files/search"

    async def paths(q: str) -> list[str]:
        response = await client.get(url, params={"q": q})
        assert response.status_code == 200, response.text
        return [hit["path"] for hit in response.json()["results"]]

    assert (await paths("app"))[0] == "src/app.py"
    assert (await paths("usrctrl"))[0] == "src/user_controller.py"  # characters in order
    assert (await paths("UserController"))[0] == "src/user_controller.py"
    assert "docs/guide.md" in await paths("guide")
    assert await paths("zzzzqqq") == []
    assert (await paths("util"))[0] == "src/lib/util.py"


GREP_FILES = {
    "src/app.py": (
        b"def main():\n    return handle_request(1)\n\n\ndef handle_request(n):\n    return n\n"
    ),
    "src/routes.ts": b"fetch('/api/v1/items')\nfetch('/api/v1/users')\n",
    "README.md": b"Call handle_request( to start.\n",
    "node_modules/dep/index.js": b"handle_request()\n",  # discovery skips dependencies
}


async def test_exact_search(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    _, headers, _ = await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, GREP_FILES, SHA_ONE)
    await _index(
        app, db_session, storage, tmp_path, {"src/app.py": b"def renamed(): ...\n"}, SHA_TWO
    )
    url = f"{API}/orgs/acme/repos/api/grep"

    async def grep(**body: Any) -> dict[str, Any]:
        response = await client.post(url, json=body, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def places(body: dict[str, Any]) -> list[tuple[str, int]]:
        return [(hit["path"], hit["line"]) for hit in body["hits"]]

    old = await grep(pattern="handle_request(", ref=SHA_ONE[:8])
    assert old["commit_sha"] == SHA_ONE
    assert places(old) == [("README.md", 1), ("src/app.py", 2), ("src/app.py", 5)]
    assert (old["truncated"], old["timed_out"], old["long_lines"]) == (False, False, 0)
    assert old["hits"][1] == {
        "path": "src/app.py",
        "line": 2,
        "col": 12,
        "text": "    return handle_request(1)",
        "offset": 0,
        "ranges": [[12, 27]],
        "before": [],
        "after": [],
    }
    regex = await grep(pattern=r"^def \w+\(", regex=True, ref=SHA_ONE, context=1)
    assert places(regex) == [("src/app.py", 1), ("src/app.py", 5)]
    assert (regex["hits"][1]["before"], regex["hits"][1]["after"]) == ([""], ["    return n"])
    globbed = await grep(pattern="/api/v1/", path_globs=["src/**", "!*.py"], ref=SHA_ONE, limit=1)
    assert (places(globbed), globbed["truncated"]) == ([("src/routes.ts", 1)], True)
    ignoring_case = await grep(pattern="HANDLE_request", case="insensitive", ref=SHA_ONE)
    assert len(ignoring_case["hits"]) == 3

    latest = await grep(pattern="handle_request")  # the last index by default
    assert (latest["commit_sha"], latest["hits"]) == (SHA_TWO, [])

    for body, code in (
        ({"pattern": "handle_request(", "regex": True}, "invalid_pattern"),
        ({"pattern": "x", "path_globs": ["["]}, "invalid_pattern"),
        ({"pattern": "a\nb"}, "invalid_pattern"),
        ({"pattern": ""}, "validation_failed"),
        ({"pattern": "x", "limit": 501}, "validation_failed"),
        ({"pattern": "x", "path_globs": ["*"] * 21}, "validation_failed"),
    ):
        refused = await client.post(url, json=body, headers=headers)
        assert (refused.status_code, refused.json()["error"]["code"]) == (422, code), body
    unknown = await client.post(url, json={"pattern": "x", "ref": "nope"}, headers=headers)
    assert unknown.json()["error"]["code"] == "ref_not_indexed"


async def test_exact_search_in_a_first_run_leaves_binaries_out(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    _, headers, job_id = await _connected(app, client, db_session, fake)
    # A binary file whose text matches before its first NUL: rg could report it; we don't.
    files = {**GREP_FILES, "assets/blob.bin": b"handle_request(\0\1\2"}
    await _index(app, db_session, storage, tmp_path, files, SHA_ONE, outcome=JobStatus.FAILED)
    # The first run is between discovery and its Merkle stage: searchable from the rows.
    snapshot = await db_session.scalar(select(Snapshot).where(Snapshot.commit_sha == SHA_ONE))
    job = await db_session.get(IndexJob, uuid.UUID(job_id))
    assert snapshot is not None
    assert job is not None
    blob = await db_session.scalar(select(RepoFile).where(RepoFile.path == "assets/blob.bin"))
    assert blob is not None
    assert "binary" in blob.flags  # discovery saw what it is
    snapshot.merkle_root = None
    job.status, job.snapshot_id = JobStatus.RUNNING, snapshot.id
    job.stages = {"discover": {"status": "done"}}
    await db_session.flush()
    response = await client.post(
        f"{API}/orgs/acme/repos/api/grep", json={"pattern": "handle_request("}, headers=headers
    )
    assert response.status_code == 200, response.text
    paths = {hit["path"] for hit in response.json()["hits"]}
    assert paths == {"README.md", "src/app.py"}


async def test_exact_search_access_and_failures(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, GREP_FILES, SHA_ONE)
    url = f"{API}/orgs/acme/repos/api/grep"
    outsider, headers = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{outsider.login}"] = set()  # can't read acme-gh/api on GitHub
    hidden = await client.post(url, json={"pattern": "items"}, headers=headers)
    assert hidden.json()["error"]["code"] == "repo_not_found_or_no_access"

    member, headers = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = {101}
    read = await client.post(url, json={"pattern": "items"}, headers=headers)
    assert (read.status_code, len(read.json()["hits"])) == (200, 1)  # members search too
    no_csrf = await client.post(url, json={"pattern": "items"})
    assert no_csrf.json()["error"]["code"] == "csrf_failed"

    app.state.exact_search = ExactSearch(binary="no-such-rg")
    broken = await client.post(url, json={"pattern": "items"}, headers=headers)
    assert (broken.status_code, broken.json()["error"]["code"]) == (503, "search_unavailable")
    busy_search = ExactSearch(timeout=0.2, max_concurrent=1)
    await busy_search._slots.acquire()  # pyright: ignore[reportPrivateUsage] - one still running
    app.state.exact_search = busy_search
    busy = await client.post(url, json={"pattern": "items"}, headers=headers)
    assert (busy.status_code, busy.json()["error"]["code"]) == (503, "search_busy")


async def test_browsing_needs_an_index_and_access(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    not_yet = await client.get(f"{API}/orgs/acme/repos/api/tree")
    assert (not_yet.status_code, not_yet.json()["error"]["code"]) == (404, "repo_not_indexed")
    await _index(app, db_session, storage, tmp_path, {"a.py": b"x\n"}, SHA_ONE)
    member, _ = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = set()  # can't read acme-gh/api on GitHub
    hidden = await client.get(f"{API}/orgs/acme/repos/api/file", params={"path": "a.py"})
    assert hidden.json()["error"]["code"] == "repo_not_found_or_no_access"


# ------------------------------------------------------------------ symbols


async def _extract_symbols(db: AsyncSession, files: dict[str, bytes], sha: str) -> None:
    """The symbols stage, on the snapshot ``_index`` left at ``sha``."""
    snapshot = await db.scalar(select(Snapshot).where(Snapshot.commit_sha == sha))
    assert snapshot is not None
    rows = await db.execute(
        select(RepoFile.path, RepoFile.id).where(RepoFile.snapshot_id == snapshot.id)
    )
    file_ids = {row.path: row.id for row in rows}
    found: list[FileSymbols] = []
    for path, data in files.items():
        result = parse_file(path, data)
        if result.ok and result.language is not None:
            symbols = extract_symbols(result, result.language)
            found.append(FileSymbols(path, result.language, symbols))
    snapshot.symbol_count = await save_symbols(db, snapshot, file_ids, found)
    await db.flush()


async def test_symbols_by_name(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    url = f"{API}/orgs/acme/repos/api/symbols"
    not_yet = await client.get(url, params={"q": "main"})
    assert (not_yet.status_code, not_yet.json()["error"]["code"]) == (409, "not_indexed")

    await _extract_symbols(db_session, FILES, SHA_ONE)
    response = await client.get(url, params={"q": " main "})
    assert response.status_code == 200, response.text
    assert response.headers["x-commit-sha"] == SHA_ONE
    body = response.json()
    assert body["query"] == "main"
    assert body["results"][0] == {
        "symbol_key": "py src/app.py/main().",
        "name": "main",
        "qualified_name": "main",
        "kind": "function",
        "path": "src/app.py",
        "start_line": 1,
        "end_line": 2,
        "signature": "def main()",
        "score": 1.0,
    }
    found = (await client.get(url, params={"q": "user", "kind": "class"})).json()["results"]
    assert [(r["name"], r["kind"]) for r in found] == [("UserController", "class")]
    modules = (await client.get(url, params={"q": "util", "kind": "module"})).json()["results"]
    assert [(r["symbol_key"], r["signature"]) for r in modules] == [("py src/lib/util.py/", None)]
    one = (await client.get(url, params={"q": "a", "limit": 1})).json()["results"]
    assert len(one) == 1
    for params, status in (
        ({"q": "main", "limit": 51}, 422),
        ({"q": "main", "kind": "struct"}, 422),
        ({"q": ""}, 422),
    ):
        assert (await client.get(url, params=params)).status_code == status, params
    blank = await client.get(url, params={"q": "   "})
    assert (blank.status_code, blank.json()["error"]["code"]) == (422, "invalid_query")
    # An older commit indexed before symbols existed.
    await _index(app, db_session, storage, tmp_path, FILES, SHA_TWO)
    old = await client.get(url, params={"q": "main", "ref": SHA_ONE[:8]})
    assert (old.status_code, old.headers["x-commit-sha"]) == (200, SHA_ONE)
    latest = await client.get(url, params={"q": "main"})
    assert latest.json()["error"]["code"] == "not_indexed"


async def test_the_symbols_at_a_line(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    url = f"{API}/orgs/acme/repos/api/symbols/at"
    not_yet = await client.get(url, params={"path": "src/app.py", "line": 2})
    assert (not_yet.status_code, not_yet.json()["error"]["code"]) == (409, "not_indexed")

    await _extract_symbols(db_session, FILES, SHA_ONE)
    response = await client.get(url, params={"path": "src/app.py", "line": 2})
    assert response.status_code == 200, response.text
    assert response.headers["x-commit-sha"] == SHA_ONE
    body = response.json()
    assert (body["path"], body["line"]) == ("src/app.py", 2)
    assert [(s["symbol_key"], s["kind"]) for s in body["symbols"]] == [
        ("py src/app.py/main().", "function"),
        ("py src/app.py/", "module"),
    ]
    assert body["symbols"][0] == {
        "symbol_key": "py src/app.py/main().",
        "name": "main",
        "qualified_name": "main",
        "kind": "function",
        "start_line": 1,
        "end_line": 2,
        "signature": "def main()",
        "docstring": None,
    }
    readme = await client.get(url, params={"path": "README.md", "line": 1})
    assert readme.json()["symbols"] == []
    missing = await client.get(url, params={"path": "src/nope.py", "line": 1})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "path_not_found")
    outside = await client.get(url, params={"path": "../etc/passwd", "line": 1})
    assert outside.status_code == 400
    assert (await client.get(url, params={"path": "src/app.py", "line": 0})).status_code == 422


async def test_symbols_need_access(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    await _extract_symbols(db_session, FILES, SHA_ONE)
    member, _ = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = set()  # can't read acme-gh/api on GitHub
    for path, params in (
        ("symbols", {"q": "main"}),
        ("symbols/at", {"path": "src/app.py", "line": 1}),
    ):
        hidden = await client.get(f"{API}/orgs/acme/repos/api/{path}", params=params)
        assert hidden.json()["error"]["code"] == "repo_not_found_or_no_access", path
        assert "x-commit-sha" not in hidden.headers


# ------------------------------------------------------------------ search

SEARCH_FILES = {
    "backend/app/api/routes/items.py": (
        b"from app.services.items import create_item\n"
        b"\n"
        b"\n"
        b"@router.post('/items/')\n"
        b"def create_item_route(item_in):\n"
        b'    """Create a new item."""\n'
        b"    return create_item(item_in)\n"
    ),
    "backend/app/services/items.py": (
        b"def create_item(item_in):\n"
        b"    item = Item(title=item_in.title)\n"
        b"    session.add(item)\n"
        b"    return item\n"
        b"\n"
        b"\n"
        b"def delete_item(item_id):\n"
        b"    item = session.get(Item, item_id)\n"
        b"    session.delete(item)\n"
    ),
    "backend/app/models.py": b"class Item:\n    title: str\n    owner_id: int\n",
    "frontend/src/routes/items.tsx": (
        b"export function ItemsPage() {\n"
        b"  const create = () => createItem({ title: 'new item' })\n"
        b"  return <button onClick={create}>New item</button>\n"
        b"}\n"
    ),
    "backend/tests/test_items.py": b"def test_create_item():\n    assert create_item(1)\n",
    "README.md": b"# Items\n\nCreate an item with POST /items/.\n",
}


async def _chunk(db: AsyncSession, files: dict[str, bytes], sha: str) -> None:
    """The symbols and chunks stages, on the snapshot ``_index`` left at ``sha``."""
    await _extract_symbols(db, files, sha)
    snapshot = await db.scalar(select(Snapshot).where(Snapshot.commit_sha == sha))
    assert snapshot is not None
    rows = await db.execute(
        select(RepoFile.path, RepoFile.id, RepoFile.language).where(
            RepoFile.snapshot_id == snapshot.id
        )
    )
    found = {row.path: (row.id, row.language) for row in rows}
    chunks = [
        FileChunks(path, found[path][1], chunk_file(path, found[path][1], data))
        for path, data in files.items()
        if path in found
    ]
    await save_chunks(db, snapshot, {path: ids[0] for path, ids in found.items()}, chunks)
    await db.flush()


@pytest.fixture
def searching(app: FastAPI) -> SearchService:
    """Search by the rules, in the fused order (no model, no reranker)."""
    service = SearchService()
    app.state.search_service = service
    return service


async def _searchable_repo(
    app: FastAPI,
    client: httpx.AsyncClient,
    db: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> dict[str, str]:
    _, headers, _ = await _connected(app, client, db, fake)
    await _index(app, db, storage, tmp_path, SEARCH_FILES, SHA_ONE)
    await _chunk(db, SEARCH_FILES, SHA_ONE)
    return headers


SEARCH_URL = f"{API}/orgs/acme/repos/api/search"


async def test_search_answers_by_lane(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
    searching: SearchService,
) -> None:
    _, headers, _ = await _connected(app, client, db_session, fake)
    never = await client.post(SEARCH_URL, json={"q": "item"}, headers=headers)
    assert (never.status_code, never.json()["error"]["code"]) == (404, "repo_not_indexed")
    await _index(app, db_session, storage, tmp_path, SEARCH_FILES, SHA_ONE)
    not_yet = await client.post(SEARCH_URL, json={"q": "item"}, headers=headers)
    assert (not_yet.status_code, not_yet.json()["error"]["code"]) == (409, "not_indexed")
    await _chunk(db_session, SEARCH_FILES, SHA_ONE)

    response = await client.post(
        SEARCH_URL, json={"q": "how is a new item created?"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-commit-sha"] == SHA_ONE
    body = response.json()
    assert body["query"] == "how is a new item created?"
    assert set(body["lanes"]) == set(body["counts"]) == set(body["next"]) == set(LANES)
    assert body["partial"] == {"semantic_pending": True, "summaries_pending": True}
    assert body["interpretation"]["class"] in ("conceptual", "flow")
    hits = {hit["id"]: hit for lane in body["lanes"].values() for hit in lane}
    service = hits["py backend/app/services/items.py/create_item()."]
    assert service in body["lanes"]["svc"]
    assert (service["name"], service["kind"], service["layer"]) == (
        "create_item",
        "function",
        "svc",
    )
    assert (service["start_line"], service["end_line"]) == (1, 4)
    assert service["snippet"]["start_line"] == 1
    assert service["snippet"]["lines"][0] == "def create_item(item_in):"
    assert {"line": 1, "start": 11, "end": 15} in service["snippet"]["highlights"]  # "item"
    assert "keyword" in service["why"]
    handler = hits["py backend/app/api/routes/items.py/create_item_route()."]
    assert handler in body["lanes"]["api"]
    assert any(hit["layer"] == "db" for hit in body["lanes"]["db"])
    assert any(hit["kind"] == "file" and hit["symbol_key"] is None for hit in hits.values())
    for lane, found in body["lanes"].items():
        assert body["counts"][lane] >= len(found)
        for hit in found:
            assert 0 <= hit["score"] <= 1
            assert hit["why"]
            assert len(hit["snippet"]["lines"]) <= 12
    scores = [hit["score"] for lane in body["lanes"].values() for hit in lane]
    assert max(scores) == 1.0


async def test_search_pages_a_lane_with_its_cursor(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
    searching: SearchService,
) -> None:
    headers = await _searchable_repo(app, client, db_session, fake, storage, tmp_path)
    first = (await client.post(SEARCH_URL, json={"q": "item", "limit": 1}, headers=headers)).json()
    lane = next(lane for lane in LANES if first["counts"][lane] > 1)
    assert len(first["lanes"][lane]) == 1
    cursor = first["next"][lane]
    assert cursor
    seen = [hit["id"] for hit in first["lanes"][lane]]
    while cursor:
        page = await client.post(
            SEARCH_URL,
            json={"q": "item", "limit": 1, "layers": [lane], "cursor": cursor},
            headers=headers,
        )
        assert page.status_code == 200, page.text
        more = page.json()
        assert more["counts"] == first["counts"]
        assert all(not more["lanes"][other] for other in LANES if other != lane)
        seen += [hit["id"] for hit in more["lanes"][lane]]
        cursor = more["next"][lane]
    assert len(seen) == len(set(seen)) == first["counts"][lane]

    for body in (
        {"q": "item", "cursor": "nope"},
        {"q": "other words", "cursor": first["next"][lane]},  # another search's
        {"q": "item", "layers": [o for o in LANES if o != lane], "cursor": first["next"][lane]},
    ):
        bad = await client.post(SEARCH_URL, json=body, headers=headers)
        assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_cursor"), body


async def test_search_modes_filters_and_exact_pivots(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
    searching: SearchService,
) -> None:
    headers = await _searchable_repo(app, client, db_session, fake, storage, tmp_path)

    async def search(**body: Any) -> httpx.Response:
        return await client.post(SEARCH_URL, json=body, headers=headers)

    semantic = await search(q="item", mode="semantic")
    assert (semantic.status_code, semantic.json()["error"]["code"]) == (409, "semantic_not_ready")
    keyword = (await search(q="item", mode="keyword")).json()
    assert all("semantic" not in hit["why"] for lane in keyword["lanes"].values() for hit in lane)
    # A literal: the exact leg finds the line, reported as the definition around it.
    literal = (await search(q='where is "session.delete(item)" called')).json()
    assert {"kind": "literal", "query": "session.delete(item)"} in literal["exact_queries"]
    assert literal["interpretation"]["literals"] == ["session.delete(item)"]
    first = literal["lanes"]["svc"][0]
    assert first["id"] == "py backend/app/services/items.py/delete_item()."
    assert (first["start_line"], first["end_line"]) == (7, 9)
    symbol = (await search(q="create_item_route")).json()
    assert symbol["interpretation"]["class"] == "symbol"
    assert {"kind": "symbol", "query": "create_item_route"} in symbol["exact_queries"]
    # Filters hold on every leg; the query's "not tests" too.
    scoped = (await search(q="item", path="backend/app/")).json()
    paths = {hit["path"] for lane in scoped["lanes"].values() for hit in lane}
    assert paths
    assert all(path.startswith("backend/app/") for path in paths)
    assert scoped["interpretation"]["path_filter"] == "backend/app/"
    typescript = (await search(q="item", langs=["typescript"])).json()
    assert {hit["language"] for lane in typescript["lanes"].values() for hit in lane} <= {
        "typescript"
    }
    no_tests = (await search(q="create item (not tests)")).json()
    assert all("tests/" not in hit["path"] for hit in no_tests["lanes"]["other"])
    # Only the lanes asked for.
    api_only = (await search(q="item", layers=["api"])).json()
    assert api_only["lanes"]["api"]
    assert all(not api_only["lanes"][lane] for lane in LANES if lane != "api")

    for body, code in (
        ({"q": "   "}, "invalid_query"),
        ({"q": "x" * 501}, "validation_failed"),
        ({"q": "item", "limit": 51}, "validation_failed"),
        ({"q": "item", "layers": ["service"]}, "validation_failed"),
        ({"q": "item", "mode": "fuzzy"}, "validation_failed"),
        ({"q": "item", "path": "../etc"}, "validation_failed"),
        ({"q": "item", "extra": 1}, "validation_failed"),
    ):
        refused = await search(**body)
        assert (refused.status_code, refused.json()["error"]["code"]) == (422, code), body


async def test_search_is_rate_limited_per_user(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
    searching: SearchService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await _searchable_repo(app, client, db_session, fake, storage, tmp_path)
    monkeypatch.setattr(app.state.settings.search, "rate_per_minute", 2)
    for _ in range(2):
        ok = await client.post(SEARCH_URL, json={"q": "item"}, headers=headers)
        assert ok.status_code == 200, ok.text
    limited = await client.post(SEARCH_URL, json={"q": "item"}, headers=headers)
    assert (limited.status_code, limited.json()["error"]["code"]) == (429, "rate_limited")
    assert 1 <= int(limited.headers["retry-after"]) <= 30
    # Another user has a bucket of their own.
    member, member_headers = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = {101}
    other = await client.post(SEARCH_URL, json={"q": "item"}, headers=member_headers)
    assert other.status_code == 200, other.text


async def test_search_needs_access(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
    searching: SearchService,
) -> None:
    await _searchable_repo(app, client, db_session, fake, storage, tmp_path)
    member, member_headers = await _member(app, client, db_session)
    fake.user_can_read[f"ghu_{member.login}"] = set()  # can't read acme-gh/api on GitHub
    hidden = await client.post(SEARCH_URL, json={"q": "item"}, headers=member_headers)
    assert hidden.json()["error"]["code"] == "repo_not_found_or_no_access"
    assert "x-commit-sha" not in hidden.headers


async def test_repo_rows_carry_index_summaries(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    _, _, job_id = await _connected(app, client, db_session, fake)
    queued = (await client.get(f"{API}/orgs/acme/repos")).json()[0]
    assert queued["active_job"]["id"] == job_id
    assert (queued["active_job"]["status"], queued["file_count"]) == ("queued", None)
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    row = (await client.get(f"{API}/orgs/acme/repos/api")).json()
    assert row["file_count"] == len(FILES)
    assert row["loc"] > 0
    assert row["last_indexed_at"] is not None


# ------------------------------------------------------------------ the constellation


async def test_the_last_index_constellation_in_pages(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    await _connected(app, client, db_session, fake)
    url = f"{API}/orgs/acme/repos/api/constellation"
    not_yet = await client.get(url)
    assert (not_yet.status_code, not_yet.json()["error"]["code"]) == (404, "repo_not_indexed")
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)

    # Indexed before constellations were stored: laid out from the file rows, the same way.
    whole = await client.get(url)
    assert whole.status_code == 200, whole.text
    body = whole.json()
    assert (body["status"], body["layout"], body["commit_sha"]) == (
        "complete",
        "directory",
        SHA_ONE,
    )
    assert (body["total"], body["available"], body["since"], body["next"]) == (8, 8, 0, 8)
    assert [p["path"] for p in body["points"]] == sorted(FILES)
    rows = {row.path: row for row in await db_session.scalars(select(RepoFile))}
    assert {p["path"]: p["file_id"] for p in body["points"]} == {
        path: str(rows[path].id) for path in FILES
    }
    dirs = {d["id"]: d for d in body["dirs"]}
    assert dirs[0]["path"] == ""
    by_path = {p["path"]: p for p in body["points"]}
    # Each point carries its row's role, drawn in that role's layer.
    assert {path: (p["role"], p["layer"]) for path, p in by_path.items()} == {
        path: (rows[path].role, layer_for(rows[path].role)) for path in FILES
    }
    assert (by_path["src/app.py"]["layer"], by_path["README.md"]["layer"]) == ("svc", "other")
    assert dirs[by_path["src/lib/util.py"]["dir_cluster"]]["path"] == "src/lib"
    assert all(math.dist((0, 0, 0), (p["x"], p["y"], p["z"])) < 1 for p in body["points"])

    first = (await client.get(url, params={"limit": 3})).json()
    rest = (await client.get(url, params={"since": first["next"]})).json()
    assert (first["next"], len(first["dirs"]), rest["dirs"]) == (3, len(body["dirs"]), [])
    assert first["points"] + rest["points"] == body["points"]

    # Once stored (by discovery), the stored one answers.
    repo = await db_session.scalar(select(Repo).where(Repo.slug == "api"))
    assert repo is not None
    layout = DirectoryLayout(["only/one.py"])
    stored = Constellation.build(
        "gen-stored",
        layout,
        [
            layout.point(
                file_id=uuid.uuid4(),
                path="only/one.py",
                role="db",
                language="python",
                size=3,
                loc=1,
            )
        ],
    )
    key = artifact_key(repo.org_id, repo.id, SHA_ONE, *artifact_parts("gen-stored"))
    await storage.put_bytes(Bucket.ARTIFACTS, key, stored.to_bytes())
    # Stored, but the snapshot's rows don't name it (that discovery never committed): unused.
    orphan = (await client.get(url)).json()
    assert orphan["generation"] == body["generation"]
    snapshot = await db_session.scalar(select(Snapshot).where(Snapshot.commit_sha == SHA_ONE))
    assert snapshot is not None
    snapshot.summary = {**(snapshot.summary or {}), "constellation": {"generation": "gen-stored"}}
    await db_session.flush()
    again = (await client.get(url)).json()
    assert (again["generation"], [p["path"] for p in again["points"]]) == (
        "gen-stored",
        ["only/one.py"],
    )
    assert again["points"][0]["layer"] == "db"
    # A damaged stored one: laid out from the rows instead, not an error.
    bad = artifact_key(repo.org_id, repo.id, SHA_ONE, *artifact_parts("gen-bad"))
    await storage.put_bytes(Bucket.ARTIFACTS, bad, b"not a constellation")
    snapshot.summary = {**(snapshot.summary or {}), "constellation": {"generation": "gen-bad"}}
    await db_session.flush()
    damaged = await client.get(url)
    assert damaged.status_code == 200
    assert damaged.json()["generation"] == body["generation"]
    both = await client.get(url, params={"ref": SHA_ONE, "job": str(uuid.uuid4())})
    assert (both.status_code, both.json()["error"]["code"]) == (422, "job_and_ref")


async def test_a_runs_constellation_forms_while_discovery_runs(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    fake_redis: FakeAsyncRedis,
    storage: ObjectStorage,
    tmp_path: Path,
) -> None:
    _, _, job_id = await _connected(app, client, db_session, fake)
    url = f"{API}/orgs/acme/repos/api/constellation"
    pending = (await client.get(url, params={"job": job_id})).json()
    assert (pending["status"], pending["points"], pending["total"]) == ("pending", [], None)

    # Discovery has walked the tree and read the first files.
    paths = [f"pkg/m{i:04}.py" for i in range(LIVE_BATCH + 20)]
    layout = DirectoryLayout(paths)
    points = [
        layout.point(file_id=uuid.uuid4(), path=p, role="service", language="python", size=1, loc=1)
        for p in paths
    ]
    live = LiveConstellation(fake_redis, uuid.UUID(job_id), layout, "gen-live")
    await live.begin()
    await live.add(points[:LIVE_BATCH])
    forming = (await client.get(url, params={"job": job_id})).json()
    assert (forming["status"], forming["generation"]) == ("streaming", "gen-live")
    assert (forming["total"], forming["available"], forming["next"]) == (
        len(paths),
        LIVE_BATCH,
        LIVE_BATCH,
    )
    assert [p["path"] for p in forming["points"]] == paths[:LIVE_BATCH]
    assert len(forming["dirs"]) == len(layout.dirs)
    await live.add(points[LIVE_BATCH:])
    await live.finish()
    more = (await client.get(url, params={"job": job_id, "since": forming["next"]})).json()
    assert [p["path"] for p in more["points"]] == paths[LIVE_BATCH:]
    assert more["dirs"] == []

    # Discovery done: the snapshot's stored constellation answers.
    await _index(app, db_session, storage, tmp_path, FILES, SHA_ONE)
    snapshot = await db_session.scalar(select(Snapshot).where(Snapshot.commit_sha == SHA_ONE))
    job = await db_session.get(IndexJob, uuid.UUID(job_id))
    assert snapshot is not None
    assert job is not None
    machine = IndexJobStateMachine(job)
    machine.start()
    machine.advance(Stage.DISCOVER)
    job.snapshot_id = snapshot.id
    machine.complete(Stage.DISCOVER)
    await db_session.flush()
    done = (await client.get(url, params={"job": job_id})).json()
    assert (done["status"], done["commit_sha"], done["total"]) == ("complete", SHA_ONE, 8)

    missing = await client.get(url, params={"job": str(uuid.uuid4())})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "index_job_not_found")


async def test_a_run_that_ended_before_discovery_finished_stops(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeGitHub,
    fake_redis: FakeAsyncRedis,
) -> None:
    _, headers, job_id = await _connected(app, client, db_session, fake)
    url = f"{API}/orgs/acme/repos/api/constellation"
    cancelled = await client.post(
        f"{API}/orgs/acme/repos/api/index-jobs/{job_id}/cancel", headers=headers
    )
    assert cancelled.status_code == 200
    stopped = (await client.get(url, params={"job": job_id})).json()
    assert (stopped["status"], stopped["points"]) == ("stopped", [])
