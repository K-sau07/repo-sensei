"""The dependency graph by level (6.2) over the API: a level with its layout (kept as an artifact
and read back the same), a focus, the evidence behind an edge, MessagePack for big levels."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import msgpack
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import IndexJob, JobKind, JobStatus, Membership, Org, Repo, User
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.orgs.service import OrgService
from sensei_testing import sign_in
from sensei_testing.indexed import ITEMS, ROUTE, SERVICE, index_repo

API = "/api/v1"
NOW = datetime(2026, 10, 31, tzinfo=UTC)


class MemoryArtifacts:
    """Artifacts kept in memory (the graph has none: it is read from its rows)."""

    def __init__(self) -> None:
        self.kept: dict[str, bytes] = {}

    async def get_bytes(self, _bucket: Any, key: str, **_: Any) -> bytes | None:
        return self.kept.get(key)

    async def put_bytes(self, _bucket: Any, key: str, data: bytes, **_: Any) -> None:
        self.kept[key] = data


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.storage = MemoryArtifacts()
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def _indexed(
    api_app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, tmp_path: Path
) -> str:
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
    return f"{API}/orgs/{org.slug}/repos/items"


@pytest.mark.integration
async def test_a_level_comes_with_its_layout_kept_and_read_back_the_same(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url = await _indexed(api_app, client, db_session, tmp_path)
    got = await client.get(f"{url}/deps", params={"level": "file"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert got.headers["x-commit-sha"] == body["sha"]
    assert (body["level"], body["layout"], body["truncated"]) == ("file", "force", False)
    files = {n["id"]: n for n in body["nodes"]}
    assert {ROUTE, SERVICE} <= set(files)
    assert files[ROUTE]["cluster"] == "backend/app/api/routes"
    [edge] = [e for e in body["edges"] if (e["source"], e["target"]) == (ROUTE, SERVICE)]
    assert edge["types"]["calls"] >= 1
    assert edge["weight"] == sum(edge["types"].values())
    assert all(-1 <= n["x"] <= 1 and -1 <= n["y"] <= 1 for n in body["nodes"])
    storage: MemoryArtifacts = api_app.state.storage
    assert any(key.endswith("layout-file.msgpack") for key in storage.kept)
    # A fresh process reads the kept layout: the same places.
    api_app.state.deps_cache = None
    again = (await client.get(f"{url}/deps", params={"level": "file"})).json()
    assert again == body
    # MessagePack, gzipped: the same answer.
    packed = await client.get(
        f"{url}/deps", params={"level": "file"}, headers={"Accept": "application/x-msgpack"}
    )
    assert packed.headers["content-type"] == "application/x-msgpack"
    assert msgpack.unpackb(packed.content) == body  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.integration
async def test_a_focus_and_the_evidence_behind_an_edge(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url = await _indexed(api_app, client, db_session, tmp_path)
    near = await client.get(f"{url}/deps", params={"level": "file", "focus": SERVICE})
    assert near.status_code == 200, near.text
    assert {n["id"] for n in near.json()["nodes"]} == {ROUTE, SERVICE}
    whole = (await client.get(f"{url}/deps", params={"level": "file"})).json()
    placed = {n["id"]: (n["x"], n["y"]) for n in whole["nodes"]}
    assert all(placed[n["id"]] == (n["x"], n["y"]) for n in near.json()["nodes"])  # same places
    missing = await client.get(f"{url}/deps", params={"level": "file", "focus": "nope.py"})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "deps_focus_not_found")
    only = await client.get(f"{url}/deps", params={"level": "file", "types": "writes"})
    assert only.json()["edges"] == []

    lines = await client.get(
        f"{url}/deps/edge-evidence", params={"level": "file", "src": ROUTE, "dst": SERVICE}
    )
    assert lines.status_code == 200, lines.text
    found = lines.json()
    assert found["total"] >= 1
    call = next(e for e in found["edges"] if e["type"] == "calls")
    assert call["source"] == "py backend/app/api/routes/items.py/create_item_route()."
    assert (call["path"], call["line"]) == (ROUTE, 7)

    symbols = (await client.get(f"{url}/deps", params={"level": "symbol"})).json()
    assert "py backend/app/services/items.py/create_item()." in {n["id"] for n in symbols["nodes"]}
    services = (await client.get(f"{url}/deps", params={"level": "service"})).json()
    assert [n["label"] for n in services["nodes"]] == ["app"]  # no Compose, no app framework
