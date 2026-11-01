"""The dependency graph by level (6.2) over the API: a level with its layout (kept as an artifact
and read back the same), a focus, the evidence behind an edge, MessagePack for big levels."""

from __future__ import annotations

import gzip
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import msgpack
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.routers import deps as deps_router
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


A = "backend/app/loop/a.py"
B = "backend/app/loop/b.py"
CYCLE = {
    A: "from app.loop.b import second\n\n\ndef first():\n    return second()\n",
    B: "from app.loop.a import first\n\n\ndef second():\n    return first()\n",
}

# Methods called on receivers of unknown types, matched by their names alone (neither file
# imports the other): guesses, a cycle only when they are counted.
GUESS_X = "backend/app/jobs/x.py"
GUESS_Y = "backend/app/mail/y.py"
GUESSED = {
    GUESS_X: (
        "class Jobs:\n    def nightly_sweep(self, mailer):\n"
        "        return mailer.compose_digest(self)\n"
    ),
    GUESS_Y: (
        "class Mailer:\n    def compose_digest(self, jobs):\n"
        "        return jobs.nightly_sweep(self)\n"
    ),
}

THIRD_PARTY = {
    "backend/pyproject.toml": (
        '[project]\nname = "app"\ndependencies = ["fastapi>=0.115", "PyYAML"]\n'
        '[dependency-groups]\ndev = ["pytest"]\n'
    ),
    "backend/uv.lock": (
        'version = 1\n[[package]]\nname = "fastapi"\nversion = "0.115.6"\n'
        '[[package]]\nname = "pyyaml"\nversion = "6.0.2"\n'
    ),
    "backend/app/main.py": (
        "import os\n\nimport requests\nimport yaml\nfrom fastapi import FastAPI\n"
    ),
}


async def _indexed(
    api_app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, tmp_path: Path
) -> str:
    snapshot, _ = await index_repo(db, tmp_path, {**ITEMS, **CYCLE, **GUESSED, **THIRD_PARTY})
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
    # One kept by other layout code (or other groups), or unreadable: laid out again.
    [kept] = [key for key in storage.kept if key.endswith("layout-file.msgpack")]
    old = {"version": "g1", "format": 1, "at": {n: [0.0, 0.0] for n in files}}
    packed_old = cast("bytes", msgpack.packb(old))  # pyright: ignore[reportUnknownMemberType]
    for stale in (gzip.compress(packed_old), b"not a layout"):
        storage.kept[kept] = stale
        api_app.state.deps_cache = None
        redrawn = await client.get(f"{url}/deps", params={"level": "file"})
        assert redrawn.status_code == 200, redrawn.text
        assert redrawn.json() == body
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
    # No Compose: the app framework's root (FastAPI's, declared in backend/).
    assert [n["label"] for n in services["nodes"]] == ["backend"]


@pytest.mark.integration
async def test_the_dsm_shows_a_cycle_and_coupling(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = await _indexed(api_app, client, db_session, tmp_path)
    got = await client.get(f"{url}/deps/dsm", params={"level": "file"})
    assert got.status_code == 200, got.text
    body = got.json()
    [cycle] = body["cycles"]
    assert cycle["nodes"] == [A, B]
    assert {(e["source"], e["target"]) for e in cycle["edges"]} == {(A, B), (B, A)}
    [cut] = cycle["breaks"]  # one of the two closes it
    assert (cut["source"], cut["target"]) in {(A, B), (B, A)}
    order = [n["id"] for n in body["order"]]
    cells = {(order[c["row"]], order[c["col"]]): c for c in body["cells"]}
    assert cells[(A, B)]["cycle"] is True
    assert cells[(ROUTE, SERVICE)]["cycle"] is False
    metrics = {n["id"]: n for n in body["order"]}
    assert (metrics[SERVICE]["afferent"], metrics[SERVICE]["efferent"]) == (1, 0)
    assert metrics[SERVICE]["instability"] == 0.0
    assert metrics[ROUTE]["instability"] == 1.0
    alone = await client.get(f"{url}/deps/cycles", params={"level": "file"})
    assert alone.json()["cycles"] == body["cycles"]
    # A module holding both: no cycle at that level.
    modules = await client.get(f"{url}/deps/cycles", params={"level": "module"})
    assert modules.json()["cycles"] == []
    # Too many groups for a matrix: a coarser level, it says.
    monkeypatch.setattr(deps_router, "MAX_DSM", 2)
    big = await client.get(f"{url}/deps/dsm", params={"level": "file"})
    assert (big.status_code, big.json()["error"]["code"]) == (422, "dsm_too_large")


@pytest.mark.integration
async def test_guessed_edges_are_left_out_unless_asked_for(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url = await _indexed(api_app, client, db_session, tmp_path)
    jobs, mail = "backend/app/jobs", "backend/app/mail"

    def pairs(body: dict[str, Any]) -> set[tuple[str, str]]:
        return {(e["source"], e["target"]) for e in body["edges"]}

    sure = (await client.get(f"{url}/deps", params={"level": "module"})).json()
    assert {jobs, mail} <= {n["id"] for n in sure["nodes"]}  # still drawn, where they were
    assert not pairs(sure) & {(jobs, mail), (mail, jobs)}
    cycles = (await client.get(f"{url}/deps/cycles", params={"level": "module"})).json()
    assert cycles["cycles"] == []
    dsm = (await client.get(f"{url}/deps/dsm", params={"level": "module"})).json()
    assert dsm["cycles"] == []

    every = {"level": "module", "min_conf": 0}
    guessed = (await client.get(f"{url}/deps", params=every)).json()
    assert {(jobs, mail), (mail, jobs)} <= pairs(guessed)
    assert {n["id"]: (n["x"], n["y"]) for n in guessed["nodes"]} == {
        n["id"]: (n["x"], n["y"]) for n in sure["nodes"]
    }
    [cycle] = (await client.get(f"{url}/deps/cycles", params=every)).json()["cycles"]
    assert cycle["nodes"] == [jobs, mail]
    evidence = await client.get(
        f"{url}/deps/edge-evidence",
        params={"level": "file", "min_conf": 0, "src": GUESS_X, "dst": GUESS_Y},
    )
    [line] = evidence.json()["edges"]
    assert line["confidence"] < 0.5
    unasked = await client.get(
        f"{url}/deps/edge-evidence", params={"level": "file", "src": GUESS_X, "dst": GUESS_Y}
    )
    assert unasked.json()["total"] == 0


@pytest.mark.integration
async def test_the_impact_of_a_change(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url = await _indexed(api_app, client, db_session, tmp_path)
    service_fn = "py backend/app/services/items.py/create_item()."
    route_fn = "py backend/app/api/routes/items.py/create_item_route()."
    by_name = await client.get(f"{url}/impact", params={"symbol": "create_item"})
    assert by_name.status_code == 200, by_name.text
    body = by_name.json()
    assert [n["key"] for n in body["changed"]] == [service_fn]
    callers = {a["key"]: a for a in body["affected"]["symbols"]}
    caller = callers[route_fn]
    assert (caller["key"], caller["via"], caller["edge"], caller["distance"]) == (
        route_fn, service_fn, "calls", 1,
    )  # fmt: skip
    assert body["heat"][service_fn] == 1.0
    assert 0 < body["heat"][route_fn] < 1
    by_file = await client.get(f"{url}/impact", params={"files": [SERVICE]})
    assert by_file.json()["affected"] == body["affected"]
    neither = await client.get(f"{url}/impact")
    assert (neither.status_code, neither.json()["error"]["code"]) == (422, "impact_change")
    nothing = await client.get(f"{url}/impact", params={"symbol": "no_such_thing"})
    assert (nothing.status_code, nothing.json()["error"]["code"]) == (404, "impact_not_found")


@pytest.mark.integration
async def test_third_party_packages_declared_and_used(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url = await _indexed(api_app, client, db_session, tmp_path)
    got = await client.get(f"{url}/deps/packages")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["inventoried"] is True
    by_name = {p["name"]: p for p in body["packages"]}
    fastapi = by_name["fastapi"]
    assert (fastapi["versions"], fastapi["dev"], fastapi["files"]) == (["0.115.6"], False, 1)
    assert fastapi["declared"] == [
        {"manifest": "backend/pyproject.toml", "root": "backend", "spec": ">=0.115",
         "version": "0.115.6", "dev": False},
    ]  # fmt: skip
    assert fastapi["used_in"] == [{"path": "backend/app/main.py", "imports": 1}]
    assert by_name["pyyaml"]["files"] == 1  # imported as yaml
    assert (by_name["pytest"]["dev"], by_name["pytest"]["files"]) == (True, 0)
    assert [p["name"] for p in body["undeclared"]] == ["requests"]  # os is the standard library
