"""The public gallery (10.2): the explore org's admins publish a repository it imported by its
public URL, and anyone, signed in or not, reads it at its last index; nothing else is shown."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import orjson
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.routers import public
from sensei_core.config import ExploreSettings
from sensei_core.db.tenancy import set_org_context
from sensei_core.errors import ForbiddenError, InvalidError
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AuditEvent,
    FeatureMap,
    IndexJob,
    JobKind,
    JobStatus,
    Membership,
    Org,
    Repo,
    RepoSource,
    RepoStatus,
    Role,
    Snapshot,
    User,
)
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.orgs.service import OrgAccess, OrgService
from sensei_core.repos.service import RepoService
from sensei_core.storage import artifact_key
from sensei_engine.llm import rollups
from sensei_testing import make_user, sign_in
from sensei_testing.indexed import ITEMS, index_repo

API = "/api/v1"
pytestmark = pytest.mark.integration


class _Storage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def get_bytes(self, bucket: str, key: str, *, max_bytes: int) -> bytes | None:
        return self.objects.get(key)


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def _indexed(api_app: FastAPI, db: AsyncSession, tmp_path: Path) -> tuple[Repo, Org]:
    """A small repository the explore org imported by URL and indexed, summarized and mapped."""
    snapshot, _ = await index_repo(db, tmp_path, ITEMS)
    snapshot.status = SnapshotStatus.READY
    snapshot.merkle_root = "00" * 32
    snapshot.ready_at = datetime.now(UTC)
    repo = await db.get(Repo, snapshot.repo_id)
    org = await db.get(Org, snapshot.org_id)
    assert repo is not None
    assert org is not None
    repo.last_indexed_sha = snapshot.commit_sha
    db.add(IndexJob(org_id=repo.org_id, repo_id=repo.id, snapshot_id=snapshot.id,
                    kind=JobKind.FULL, status=JobStatus.SUCCEEDED, stages={}))  # fmt: skip
    db.add(FeatureMap(org_id=repo.org_id, repo_id=repo.id, snapshot_id=snapshot.id,
                      key="dir:backend", path="backend", title="Backend",
                      summary="The API.", citations=[{"path": "backend/app/main.py"}]))  # fmt: skip
    await db.flush()
    rolled = {"directories": [{"path": ".", "summary": "An items service."}]}
    key = artifact_key(repo.org_id, repo.id, snapshot.commit_sha, rollups.ARTIFACT)
    api_app.state.storage = _Storage({key: orjson.dumps(rolled)})
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"explore": ExploreSettings(org=org.slug.upper())}  # any case
    )
    return repo, org


async def _owner(db: AsyncSession, org: Org) -> User:
    owner_id = await db.scalar(select(Membership.user_id).where(Membership.org_id == org.id))
    owner = await db.get(User, owner_id)
    assert owner is not None
    return owner


async def test_a_published_repository_is_read_by_anyone(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    repo, _ = await _indexed(api_app, db_session, tmp_path)
    assert (await client.get(f"{API}/public/repos")).json() == {"items": []}  # not published
    assert (await client.get(f"{API}/public/repos/o/items")).status_code == 404

    repo.published_at = datetime.now(UTC)
    await db_session.flush()
    listed = await client.get(f"{API}/public/repos")
    assert listed.status_code == 200
    assert listed.headers["cache-control"].startswith("public")
    (item,) = listed.json()["items"]
    assert item["full_name"] == "o/items"
    assert item["sha"] == repo.last_indexed_sha
    assert item["summary"] == "An items service."
    assert item["files"] is not None

    # Asked again (the same repositories at the same commits): what each does is the kept one.
    def costly(*_: object) -> None:
        raise AssertionError("worked out again")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(public, "overview_of", costly)
        assert (await client.get(f"{API}/public/repos")).json() == listed.json()

    page = await client.get(f"{API}/public/repos/O/Items")  # GitHub's names are any case
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["repo"]["owner"] == "o"
    assert body["overview"]["sha"] == repo.last_indexed_sha
    assert body["overview"]["index"] is None  # the org's run, not the page's
    assert [m["title"] for m in body["overview"]["feature_maps"]] == ["Backend"]
    maps = await client.get(f"{API}/public/repos/o/items/feature-maps")
    assert maps.status_code == 200
    assert maps.json()["maps"][0]["citations"] == [{"path": "backend/app/main.py"}]

    # Gone private on GitHub: off the gallery at once.
    repo.status = RepoStatus.ACCESS_LOST
    await db_session.flush()
    assert (await client.get(f"{API}/public/repos")).json() == {"items": []}
    assert (await client.get(f"{API}/public/repos/o/items")).status_code == 404
    # No explore org: nothing at all.
    repo.status = RepoStatus.READY
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"explore": ExploreSettings()}
    )
    await db_session.flush()
    assert (await client.get(f"{API}/public/repos")).json() == {"items": []}
    missing = await client.get(f"{API}/public/repos/o/items/feature-maps")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "public_repo_not_found"


async def test_only_the_explore_orgs_published_and_indexed_repositories_are_shown(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    repo, _ = await _indexed(api_app, db_session, tmp_path)
    repo.published_at = datetime.now(UTC)
    # Another org's, published by a write to its row: not the explore org's, never shown.
    other_owner = await make_user(db_session)
    other = await OrgService(MemoryMailer(), "http://localhost:3000").create(
        db_session, other_owner, name="Other", slug=f"other-{other_owner.github_id}"
    )
    elsewhere = Repo(org_id=other.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=12,
                     owner="o", name="else", full_name="o/else", slug="else",
                     default_branch="main", visibility="public", last_indexed_sha="c" * 40,
                     published_at=datetime.now(UTC))  # fmt: skip
    db_session.add(elsewhere)
    await db_session.flush()
    db_session.add(Snapshot(org_id=other.org.id, repo_id=elsewhere.id, ref="main",
                            commit_sha="c" * 40, status=SnapshotStatus.READY))  # fmt: skip
    await db_session.flush()
    # The explore org's own, published but never indexed: shown once it is.
    await set_org_context(db_session, repo.org_id)
    db_session.add(Repo(org_id=repo.org_id, source=RepoSource.PUBLIC_URL, github_repo_id=13,
                        owner="o", name="later", full_name="o/later", slug="later",
                        default_branch="main", visibility="public",
                        published_at=datetime.now(UTC)))  # fmt: skip
    await db_session.flush()
    items = (await client.get(f"{API}/public/repos")).json()["items"]
    assert [i["full_name"] for i in items] == ["o/items"]
    for name in ("else", "later"):
        assert (await client.get(f"{API}/public/repos/o/{name}")).status_code == 404
    assert (await client.get(f"{API}/public/repos/o/it%20ems")).status_code == 422  # no name


async def test_the_explore_orgs_admins_publish_its_public_repositories(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    repo, org = await _indexed(api_app, db_session, tmp_path)
    member = await make_user(db_session)
    db_session.add(Membership(org_id=org.id, user_id=member.id, role=Role.MEMBER))
    await db_session.flush()
    _, headers = await sign_in(api_app, client, db_session, member)
    path = f"{API}/orgs/{org.slug}/repos/items/published"
    refused = await client.put(path, headers=headers)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "insufficient_role"

    _, headers = await sign_in(api_app, client, db_session, await _owner(db_session, org))
    read = await client.get(f"{API}/orgs/{org.slug}/repos/items")
    assert read.json()["publishable"] is True  # the explore org's, imported by URL
    published = await client.put(path, headers=headers)
    assert published.status_code == 200, published.text
    first = published.json()["published_at"]
    assert first is not None
    again = await client.put(path, headers=headers)  # already: the same since
    assert again.json()["published_at"] == first
    assert len((await client.get(f"{API}/public/repos")).json()["items"]) == 1
    actions = list(await db_session.scalars(
        select(AuditEvent.action).where(AuditEvent.target_id == str(repo.id))
    ))  # fmt: skip
    assert actions.count("repo.published") == 1

    # One connected through the GitHub App isn't anyone's to read: never published.
    repo.source = RepoSource.GITHUB_APP
    repo.published_at = None
    await db_session.flush()
    # (Through the service: the API reads an app repository's access on GitHub first.)
    service = RepoService.__new__(RepoService)  # publish needs none of its clients
    access = OrgAccess(org=org, role=Role.OWNER, user=await _owner(db_session, org))
    with pytest.raises(InvalidError) as private:
        await service.publish(db_session, access, repo, explore_org=org.slug, shown=True)
    assert private.value.code == "repo_not_public"
    with pytest.raises(ForbiddenError) as elsewhere:
        await service.publish(db_session, access, repo, explore_org="someone-else", shown=True)
    assert elsewhere.value.code == "not_explore_org"
    repo.source = RepoSource.PUBLIC_URL
    repo.published_at = datetime.now(UTC)
    await db_session.flush()

    taken = await client.delete(path, headers=headers)
    assert taken.status_code == 204
    await db_session.refresh(repo)
    assert repo.published_at is None
    assert (await client.get(f"{API}/public/repos")).json() == {"items": []}
