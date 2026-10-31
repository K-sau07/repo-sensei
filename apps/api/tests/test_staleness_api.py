"""Staleness (5.5): the detection matrix, a citation at one commit against the index at another:
a file unchanged, changed, deleted, and a commit no longer kept."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    IndexJob,
    JobKind,
    JobStatus,
    Membership,
    Org,
    Repo,
    RepoFile,
    Snapshot,
    User,
)
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.orgs.service import OrgService
from sensei_testing import sign_in
from sensei_testing.indexed import ITEMS, ROUTE, SERVICE, index_repo

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


def _ready(snapshot: Snapshot) -> None:
    snapshot.status, snapshot.merkle_root, snapshot.ready_at = SnapshotStatus.READY, "00" * 32, NOW


@pytest.mark.integration
async def test_citations_are_stale_when_their_file_changed_or_went(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    cited, _ = await index_repo(db_session, tmp_path, ITEMS)
    _ready(cited)
    head = Snapshot(org_id=cited.org_id, repo_id=cited.repo_id, ref="main", commit_sha="c" * 40)
    _ready(head)
    db_session.add(head)
    await db_session.flush()
    columns = [c.name for c in RepoFile.__table__.columns if c.name not in ("id", "snapshot_id")]
    listed = ", ".join(columns)
    await db_session.execute(
        text(
            f"INSERT INTO files (snapshot_id, {listed}) "
            f"SELECT :head, {listed} FROM files WHERE snapshot_id = :cited AND path <> :gone"
        ),
        {"head": head.id, "cited": cited.id, "gone": "README.md"},
    )
    await db_session.execute(
        text("UPDATE files SET content_hash = :h WHERE snapshot_id = :s AND path = :p"),
        {"h": b"\x01" * 32, "s": head.id, "p": SERVICE},
    )
    repo = await db_session.get(Repo, cited.repo_id)
    assert repo is not None
    repo.last_indexed_sha = head.commit_sha
    db_session.add(
        IndexJob(
            org_id=repo.org_id, repo_id=repo.id, snapshot_id=head.id, kind=JobKind.INCREMENTAL,
            status=JobStatus.SUCCEEDED,
        )
    )  # fmt: skip
    await db_session.flush()
    owner = await db_session.get(
        User,
        await db_session.scalar(select(Membership.user_id).where(Membership.org_id == repo.org_id)),
    )
    org = await db_session.get(Org, repo.org_id)
    assert owner is not None
    assert org is not None
    await sign_in(api_app, client, db_session, owner)

    then = cited.commit_sha
    asked = [f"{then}:{ROUTE}", f"{then}:{SERVICE}", f"{then}:README.md", f"{'e' * 40}:{ROUTE}"]
    response = await client.get(
        f"{API}/orgs/{org.slug}/repos/items/staleness", params={"citations": asked}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sha"] == head.commit_sha
    assert [(c["path"], c["stale"], c["reason"]) for c in body["citations"]] == [
        (ROUTE, False, "unchanged"),
        (SERVICE, True, "changed"),
        ("README.md", True, "deleted"),
        (ROUTE, None, "unknown"),  # a commit never kept: can't be compared
    ]
    bad = await client.get(
        f"{API}/orgs/{org.slug}/repos/items/staleness", params={"citations": ["nope"]}
    )
    assert bad.status_code == 422
    # Hex only: a LIKE wildcard isn't a commit (it would match any).
    for cited in ("%%%%%%%:README.md", "_______:README.md", f"{'g' * 7}:{ROUTE}"):
        wild = await client.get(
            f"{API}/orgs/{org.slug}/repos/items/staleness", params={"citations": [cited]}
        )
        assert wild.status_code == 422, cited
