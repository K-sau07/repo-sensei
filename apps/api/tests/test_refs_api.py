"""A repository's refs (5.4): the default branch, its tracked branches and indexed pull
requests, each with what its index is."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import IndexJob, JobKind, JobStatus, Membership, Org, Repo, Snapshot, User
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.orgs.service import OrgService
from sensei_testing import sign_in
from sensei_testing.indexed import ITEMS, index_repo

API = "/api/v1"
NOW = datetime(2026, 10, 31, tzinfo=UTC)


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


@pytest.mark.integration
async def test_refs_say_what_each_ones_index_is(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    main, _ = await index_repo(db_session, tmp_path, ITEMS)
    main.status, main.merkle_root, main.ready_at = SnapshotStatus.READY, "00" * 32, NOW
    repo = await db_session.get(Repo, main.repo_id)
    assert repo is not None
    repo.last_indexed_sha = main.commit_sha
    repo.settings = {"tracked_branches": ["release", "web"], "index_pull_requests": True}

    def snapshot(ref: str, sha: str, **extra: object) -> Snapshot:
        found = Snapshot(
            org_id=repo.org_id, repo_id=repo.id, ref=ref, commit_sha=sha * 40,
            status=SnapshotStatus.READY, merkle_root="11" * 32,
            ready_at=NOW - timedelta(hours=1), **extra,
        )  # fmt: skip
        db_session.add(found)
        return found

    snapshot("web", "c", overlay_of=main.id)
    snapshot("pull/12", "d", overlay_of=main.id)
    db_session.add_all(
        [
            IndexJob(
                org_id=repo.org_id, repo_id=repo.id, kind=JobKind.INCREMENTAL, ref="release",
                status=JobStatus.FAILED,
            ),
            IndexJob(
                org_id=repo.org_id, repo_id=repo.id, kind=JobKind.INCREMENTAL, ref="pull/12",
                status=JobStatus.RUNNING,
            ),
        ]
    )  # fmt: skip
    await db_session.flush()
    owner_id = await db_session.scalar(
        select(Membership.user_id).where(Membership.org_id == repo.org_id)
    )
    owner = await db_session.get(User, owner_id)
    org = await db_session.get(Org, repo.org_id)
    assert owner is not None
    assert org is not None
    await sign_in(api_app, client, db_session, owner)

    response = await client.get(f"{API}/orgs/{org.slug}/repos/items/refs")
    assert response.status_code == 200, response.text
    found = {r["name"]: r for r in response.json()["refs"]}
    assert list(found) == ["main", "release", "web", "pull/12"]
    assert (found["main"]["default"], found["main"]["status"]) == (True, "indexed")
    assert found["main"]["sha"] == main.commit_sha
    assert (found["release"]["status"], found["release"]["sha"]) == ("failed", None)
    assert (found["web"]["status"], found["web"]["overlay_of"]) == ("indexed", main.commit_sha)
    assert (found["pull/12"]["kind"], found["pull/12"]["status"]) == ("pull_request", "indexing")
