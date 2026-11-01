"""The Codebase Galaxy (6.9) over the API: the structural layout from the constellation; the
semantic one read from its kept artifact, asked for (once) when the summaries are embedded but
it isn't made yet, and refused when there are no summaries to place files by."""

from __future__ import annotations

import gzip
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import msgpack
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import CodeChunk, IndexJob, JobKind, JobStatus, Membership, Org, Repo, User
from sensei_core.models.chunks import EMBEDDING_DIMENSIONS
from sensei_core.models.indexing import Snapshot, SnapshotStatus
from sensei_core.orgs.service import OrgService
from sensei_core.storage import artifact_key
from sensei_engine.galaxy import Cluster, Galaxy, Star
from sensei_testing import sign_in
from sensei_testing.indexed import ITEMS, ROUTE, SERVICE, index_repo

API = "/api/v1"
NOW = datetime(2026, 10, 31, tzinfo=UTC)
_pack = cast("Callable[[object], bytes]", msgpack.packb)  # pyright: ignore[reportUnknownMemberType]


class MemoryArtifacts:
    def __init__(self) -> None:
        self.kept: dict[str, bytes] = {}

    async def get_bytes(self, _bucket: Any, key: str, **_: Any) -> bytes | None:
        return self.kept.get(key)

    async def put_bytes(self, _bucket: Any, key: str, data: bytes, **_: Any) -> None:
        self.kept[key] = data


class Jobs:
    def __init__(self) -> None:
        self.asked: list[uuid.UUID] = []

    async def layout(self, _org: uuid.UUID, _repo: uuid.UUID, snapshot_id: uuid.UUID) -> None:
        self.asked.append(snapshot_id)


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.storage = MemoryArtifacts()
    api_app.state.galaxy_jobs = Jobs()
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def _indexed(
    api_app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, tmp_path: Path
) -> tuple[str, Snapshot]:
    snapshot, _ = await index_repo(db, tmp_path, ITEMS)
    snapshot.status, snapshot.merkle_root, snapshot.ready_at = SnapshotStatus.READY, "00" * 32, NOW
    repo = await db.get(Repo, snapshot.repo_id)
    assert repo is not None
    repo.last_indexed_sha = snapshot.commit_sha
    db.add(
        IndexJob(
            org_id=repo.org_id, repo_id=repo.id, snapshot_id=snapshot.id, kind=JobKind.FULL,
            status=JobStatus.SUCCEEDED,
            stages={"graph": {"status": "done", "details": {"version": "g1"}}},
        )
    )  # fmt: skip
    await db.flush()
    owner = await db.get(
        User, await db.scalar(select(Membership.user_id).where(Membership.org_id == repo.org_id))
    )
    org = await db.get(Org, repo.org_id)
    assert owner is not None
    assert org is not None
    await sign_in(api_app, client, db, owner)
    return f"{API}/orgs/{org.slug}/repos/items", snapshot


@pytest.mark.integration
async def test_the_galaxy_by_folder_and_by_meaning(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url, snapshot = await _indexed(api_app, client, db_session, tmp_path)
    structural = await client.get(f"{url}/galaxy", params={"layout": "structural"})
    assert structural.status_code == 200, structural.text
    body = structural.json()
    assert structural.headers["x-commit-sha"] == body["sha"] == snapshot.commit_sha
    paths = {s["path"] for s in body["stars"]}
    assert {ROUTE, SERVICE, "README.md"} <= paths  # every file, the readme too
    clusters = {c["id"]: c for c in body["clusters"]}
    assert sum(c["files"] for c in clusters.values()) == len(body["stars"])
    assert {s["cluster"] for s in body["stars"]} == set(clusters)
    assert {c["label"]: c["files"] for c in clusters.values()} == {"backend": 4, "(root)": 1}
    # A cluster's name sits on one of its stars (a folder's patch is curved: its mean is under it).
    for cluster in clusters.values():
        assert any(
            (s["x"], s["y"], s["z"]) == (cluster["x"], cluster["y"], cluster["z"])
            for s in body["stars"]
            if s["cluster"] == cluster["id"]
        )

    # Semantic: no summaries embedded here.
    none = await client.get(f"{url}/galaxy")
    assert (none.status_code, none.json()["error"]["code"]) == (409, "semantic_not_ready")

    # Embedded but not laid out: asked for, once, and said so.
    some = await db_session.scalar(
        select(CodeChunk.id).where(CodeChunk.snapshot_id == snapshot.id).limit(1)
    )
    await db_session.execute(
        update(CodeChunk)
        .where(CodeChunk.id == some)
        .values(view="summary", embedding=[0.1] * EMBEDDING_DIMENSIONS)
    )
    await db_session.flush()
    jobs: Jobs = api_app.state.galaxy_jobs
    for _ in range(2):
        waiting = await client.get(f"{url}/galaxy", params={"layout": "semantic"})
        assert (waiting.status_code, waiting.json()["error"]["code"]) == (409, "galaxy_computing")
        assert int(waiting.headers["Retry-After"]) > 0
    assert jobs.asked == [snapshot.id]

    # Laid out and kept: served, each star with its file's layer.
    route_file = ROUTE
    galaxy = Galaxy([Star(route_file, 0.5, -0.5, 0.0, 0)], [Cluster(0, "items", 0.5, -0.5, 0.0, 1)])
    storage: MemoryArtifacts = api_app.state.storage
    name = artifact_key(
        snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, "galaxy-semantic.msgpack"
    )
    storage.kept[name] = gzip.compress(_pack(galaxy.as_json()))
    semantic = await client.get(f"{url}/galaxy")
    assert semantic.status_code == 200, semantic.text
    [star] = semantic.json()["stars"]
    assert (star["path"], star["x"], star["cluster"], star["layer"]) == (route_file, 0.5, 0, "api")
    assert semantic.json()["clusters"][0]["label"] == "items"
