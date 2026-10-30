"""A repository's overview (4.13) through the API: what it does (its rollups), its modules and
the graph between them, questions, health and the last index, from a small repository indexed
for real."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.routers.overview import modules_of
from sensei_core.mail import MemoryMailer
from sensei_core.models import IndexJob, JobKind, JobStatus, Membership, Org, Repo, User
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.orgs.service import OrgService
from sensei_core.storage import artifact_key
from sensei_engine.llm import rollups
from sensei_testing import sign_in
from sensei_testing.indexed import ITEMS, index_repo

API = "/api/v1"


def test_modules_are_read_at_the_shallowest_level_that_separates_them() -> None:
    one_top = [f"backend/app/{d}/{n}.py" for d in ("api", "core", "models") for n in range(3)]
    depth, module_of = modules_of([*one_top, "README.md"])
    assert depth == 3  # "backend" and "backend/app" each hold nearly everything
    assert module_of["backend/app/api/0.py"] == "backend/app/api"
    assert module_of["README.md"] == rollups.ROOT
    spread = ["web/a.ts", "web/b.ts", "api/c.py", "api/d.py", "db/e.sql"]
    assert modules_of(spread) == (1, {p: p.split("/")[0] for p in spread})


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


@pytest.mark.integration
async def test_an_indexed_repository_has_its_overview(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, _ = await index_repo(db_session, tmp_path, ITEMS)
    snapshot.status = SnapshotStatus.READY
    snapshot.merkle_root = "00" * 32
    snapshot.ready_at = datetime.now(UTC)
    repo = await db_session.get(Repo, snapshot.repo_id)
    assert repo is not None
    repo.last_indexed_sha = snapshot.commit_sha
    stages: dict[str, Any] = {
        "parse": {"status": "done"},
        "graph": {"status": "done", "details": {"unresolved_edges": 3}},
        "links": {
            "status": "done",
            "details": {"unresolved_edges": 1, "linkers": {"urls": {"calls": 4, "linked": 3}}},
        },
    }
    db_session.add(
        IndexJob(
            org_id=repo.org_id,
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            kind=JobKind.FULL,
            status=JobStatus.SUCCEEDED,
            stages=stages,
        )
    )
    await db_session.flush()
    rolled = {
        "directories": [
            {"path": ".", "summary": "An items service: a route that creates items."},
            {"path": "backend", "summary": "The API and its services."},
            {"path": "backend/app", "summary": "The app."},
        ]
    }
    key = artifact_key(repo.org_id, repo.id, snapshot.commit_sha, rollups.ARTIFACT)
    api_app.state.storage = _Storage({key: orjson.dumps(rolled)})
    owner_id = await db_session.scalar(
        select(Membership.user_id).where(Membership.org_id == repo.org_id)
    )
    owner = await db_session.get(User, owner_id)
    assert owner is not None
    await sign_in(api_app, client, db_session, owner)
    found_org = await db_session.get(Org, repo.org_id)
    assert found_org is not None

    response = await client.get(f"{API}/orgs/{found_org.slug}/repos/items/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sha"] == snapshot.commit_sha
    assert body["summary"] == {
        "text": "An items service: a route that creates items.",
        "directories": [{"path": "backend", "summary": "The API and its services."}],
    }
    modules = {m["path"]: m for m in body["architecture"]["modules"]}
    assert "backend/app/api" in modules
    assert modules["backend/app/api"]["layers"] == {"api": 1}
    links = {(link["source"], link["target"]) for link in body["architecture"]["links"]}
    assert ("backend/app/api", "backend/app/services") in links  # the route calls the service
    health = body["health"]
    python = next(lang for lang in health["languages"] if lang["language"] == "python")
    assert python["files"] == 3
    assert python["with_symbols"] == 3
    assert health["edges"]["total"] > 0
    assert health["edges"]["total"] == sum(
        health["edges"][k] for k in ("precise", "heuristic", "inferred")
    )
    assert health["links"] == {"client_calls": 4, "linked": 3, "unresolved_edges": 4}
    assert health["freshness"]["behind"] is None  # not tracked yet: never made up
    assert body["questions"]  # where it starts, at least
    assert [s["name"] for s in body["index"]["stages"]] == ["parse", "graph", "links"]

    # The snapshot doesn't change once indexed: the next look is the one kept, not read again.
    api_app.state.storage = _Storage({})
    again = await client.get(f"{API}/orgs/{found_org.slug}/repos/items/overview")
    assert again.json() == body


@pytest.mark.integration
async def test_an_overview_without_summaries_says_none(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, _ = await index_repo(db_session, tmp_path, ITEMS)
    snapshot.status = SnapshotStatus.READY
    snapshot.merkle_root = "00" * 32
    repo = await db_session.get(Repo, snapshot.repo_id)
    assert repo is not None
    repo.last_indexed_sha = snapshot.commit_sha
    db_session.add(
        IndexJob(
            org_id=repo.org_id,
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            kind=JobKind.FULL,
            status=JobStatus.SUCCEEDED,
        )
    )
    await db_session.flush()
    api_app.state.storage = _Storage({})
    owner_id = await db_session.scalar(
        select(Membership.user_id).where(Membership.org_id == repo.org_id)
    )
    owner = await db_session.get(User, owner_id)
    assert owner is not None
    await sign_in(api_app, client, db_session, owner)
    found_org = await db_session.get(Org, repo.org_id)
    assert found_org is not None
    body = (await client.get(f"{API}/orgs/{found_org.slug}/repos/items/overview")).json()
    assert body["summary"] is None
    assert body["health"]["links"] == {
        "client_calls": None,
        "linked": None,
        "unresolved_edges": None,
    }
