"""Diagrams (6.1): drawn from a trace or a question, checked against the graph, kept, listed,
renamed, shared and deleted; the model's answer sent back when it invents an edge."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    CodeNode,
    IndexJob,
    JobKind,
    JobStatus,
    Membership,
    Org,
    Repo,
    RepoSource,
    Role,
    Snapshot,
    UsageEvent,
    User,
)
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in
from sensei_testing.anthropic import FakeAnthropic, message
from sensei_testing.indexed import ITEMS, index_repo

API = "/api/v1"
NOW = datetime(2026, 10, 31, tzinfo=UTC)
ROUTE_FN = "py backend/app/api/routes/items.py/create_item_route()."
SERVICE_FN = "py backend/app/services/items.py/create_item()."


class NoArtifacts:
    """Storage with no graph artifact: the graph is read from its rows."""

    async def get_bytes(self, *_: Any, **__: Any) -> bytes | None:
        return None

    async def put_bytes(self, *_: Any, **__: Any) -> None:
        return None


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.storage = NoArtifacts()
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def _indexed(
    api_app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, tmp_path: Path
) -> tuple[str, Snapshot, dict[str, str]]:
    """ITEMS indexed, its graph done; the org's owner signed in. The repository's URL, its
    snapshot and the CSRF headers."""
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
    _, headers = await sign_in(api_app, client, db, owner)
    return f"{API}/orgs/{org.slug}/repos/items", snapshot, headers


async def _owner(db: AsyncSession, repo: Repo) -> User:
    owner = await db.get(
        User, await db.scalar(select(Membership.user_id).where(Membership.org_id == repo.org_id))
    )
    assert owner is not None
    return owner


@pytest.mark.integration
async def test_a_diagram_drawn_from_a_trace_is_kept_listed_renamed_shared_and_deleted(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url, snapshot, headers = await _indexed(api_app, client, db_session, tmp_path)
    made = await client.post(
        f"{url}/diagrams",
        json={"from_trace": {"from": "create_item_route", "direction": "down"}, "kind": "sequence"},
        headers=headers,
    )
    assert made.status_code == 201, made.text
    body = made.json()
    # No model configured here: the trace's subgraph drawn as it is, every edge the graph's.
    assert (body["source"], body["kind"], body["commit_sha"]) == (
        "trace", "sequence", snapshot.commit_sha,
    )  # fmt: skip
    assert body["verified_ratio"] == 1.0
    assert [n["id"] for n in body["spec"]["nodes"]] == [ROUTE_FN, SERVICE_FN]
    [edge] = body["spec"]["edges"]
    assert (edge["from"], edge["to"], edge["type"], edge["verified"]) == (
        ROUTE_FN, SERVICE_FN, "calls", True,
    )  # fmt: skip
    assert body["spec"]["nodes"][0]["citation"]["path"] == "backend/app/api/routes/items.py"
    assert body["mermaid"].startswith("sequenceDiagram\n")
    assert f"/code/backend/app/api/routes/items.py?ref={snapshot.commit_sha}" in body["mermaid"]
    diagram = f"{url}/diagrams/{body['id']}"

    listed = (await client.get(f"{url}/diagrams")).json()["diagrams"]
    assert [(d["id"], d["nodes"], d["edges"]) for d in listed] == [(body["id"], 2, 1)]
    renamed = await client.patch(diagram, json={"title": "  Creating   an item "}, headers=headers)
    assert renamed.json()["title"] == "Creating an item"
    assert (await client.get(diagram)).json()["spec"]["title"] == "Creating an item"

    shared = await client.post(f"{diagram}/share", headers=headers)
    assert shared.status_code == 200, shared.text
    public = await client.get(f"{API}{shared.json()['path']}")
    assert public.status_code == 200, public.text
    assert public.json()["redacted"] is False  # a public repository: anyone may read it
    assert public.json()["diagram"]["title"] == "Creating an item"
    # A private one, to someone signed out: the diagram, nothing that names its code.
    repo = await db_session.get(Repo, snapshot.repo_id)
    assert repo is not None
    repo.source = RepoSource.GITHUB_APP
    await db_session.flush()
    client.cookies.clear()
    outsider = await client.get(f"{API}{shared.json()['path']}")
    assert outsider.status_code == 200, outsider.text
    seen = outsider.json()
    assert (seen["redacted"], seen["repo"]) == (True, None)
    shown = seen["diagram"]
    assert [n["id"] for n in shown["spec"]["nodes"]] == ["n1", "n2"]
    assert [(e["from"], e["to"]) for e in shown["spec"]["edges"]] == [("n1", "n2")]
    assert [n["citation"] for n in shown["spec"]["nodes"]] == [None, None]
    assert (shown["commit_sha"], shown["created_by"], shown["prompt"]) == ("", None, {})
    assert "backend/" not in outsider.text
    assert snapshot.commit_sha not in outsider.text
    repo.source = RepoSource.PUBLIC_URL
    await db_session.flush()
    _, headers = await sign_in(api_app, client, db_session, await _owner(db_session, repo))
    await client.delete(f"{diagram}/share", headers=headers)
    assert (await client.get(f"{API}{shared.json()['path']}")).status_code == 404

    assert (await client.delete(diagram, headers=headers)).status_code == 204
    assert (await client.get(diagram)).json()["error"]["code"] == "diagram_not_found"


@pytest.mark.integration
async def test_the_models_invented_edge_is_sent_back_and_its_use_is_in_the_ledger(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url, _, headers = await _indexed(api_app, client, db_session, tmp_path)
    answers: list[dict[str, Any]] = [
        {  # n2 -> n1 is the other way in the graph
            "title": "Items",
            "nodes": [{"id": "n1", "label": "the route"}, {"id": "n2", "label": "saves it"}],
            "edges": [{"source": "n2", "target": "n1", "label": "x", "inferred_reason": ""}],
        },
        {
            "title": "Creating an item",
            "nodes": [{"id": "n1", "label": "the route"}, {"id": "n2", "label": "saves it"}],
            "edges": [
                {"source": "n1", "target": "n2", "label": "creates", "inferred_reason": ""},
                {"source": "n2", "target": "n1", "label": "done", "inferred_reason": "returns"},
            ],
        },
    ]
    sent: list[str] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content)["messages"][0]["content"])
        return httpx2.Response(200, json=message(json.dumps(answers.pop(0))))

    api_app.state.ask_client = FakeAnthropic(routes={"/v1/messages": reply}).client()
    made = await client.post(
        f"{url}/diagrams",
        json={"from_trace": {"from": "create_item_route", "direction": "down"}},
        headers=headers,
    )
    assert made.status_code == 201, made.text
    body = made.json()
    assert len(sent) == 2
    assert "is the other way in the graph" in sent[1]
    assert body["title"] == "Creating an item"
    edges: list[dict[str, Any]] = body["spec"]["edges"]
    assert [(e["verified"], e["inferred"]) for e in edges] == [
        (True, None),
        (False, {"reason": "returns"}),
    ]
    assert body["verified_ratio"] == 0.5
    assert "-.->" in body["mermaid"]  # the inferred edge, dashed
    used = list(await db_session.scalars(select(UsageEvent).where(UsageEvent.ref_id == body["id"])))
    assert [u.ref_type for u in used] == ["diagram", "diagram"]


@pytest.mark.integration
async def test_what_a_diagram_is_drawn_from_and_who_may_change_it(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url, snapshot, headers = await _indexed(api_app, client, db_session, tmp_path)
    both = await client.post(
        f"{url}/diagrams",
        json={"question": "items", "from_trace": {"from": "create_item_route"}},
        headers=headers,
    )
    assert (both.status_code, both.json()["error"]["code"]) == (422, "diagram_source")
    nothing = await client.post(
        f"{url}/diagrams", json={"question": "zebra quantum walrus"}, headers=headers
    )
    assert (nothing.status_code, nothing.json()["error"]["code"]) == (404, "diagram_nothing_found")
    asked = await client.post(
        f"{url}/diagrams", json={"question": "create item", "kind": "flowchart"}, headers=headers
    )
    assert asked.status_code == 201, asked.text
    assert asked.json()["source"] == "question"
    assert asked.json()["prompt"] == {"question": "create item"}
    assert asked.json()["mermaid"].startswith("flowchart LR\n")
    diagram = f"{url}/diagrams/{asked.json()['id']}"

    # Another member reads it, but only who drew it (or an admin) changes it.
    member = await make_user(db_session)
    db_session.add(Membership(org_id=snapshot.org_id, user_id=member.id, role=Role.MEMBER))
    await db_session.flush()
    _, theirs = await sign_in(api_app, client, db_session, member)
    assert (await client.get(diagram)).status_code == 200
    denied = await client.delete(diagram, headers=theirs)
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "diagram_not_yours")
    assert (await client.post(f"{diagram}/share", headers=theirs)).status_code == 403


@pytest.mark.integration
async def test_drawing_is_limited_per_minute(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, _, headers = await _indexed(api_app, client, db_session, tmp_path)
    monkeypatch.setattr(api_app.state.settings.agent, "diagrams_per_minute", 1)
    asked = {"from_trace": {"from": "create_item_route", "direction": "down"}}
    first = await client.post(f"{url}/diagrams", json=asked, headers=headers)
    assert first.status_code == 201, first.text
    again = await client.post(f"{url}/diagrams", json=asked, headers=headers)
    assert again.status_code == 429
    assert int(again.headers["Retry-After"]) >= 1


@pytest.mark.integration
async def test_the_whole_apps_architecture_is_named_by_the_model_and_lined_by_the_graph(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url, _, headers = await _indexed(api_app, client, db_session, tmp_path)
    sent: list[dict[str, Any]] = []
    answer = {
        "name": "Items", "summary": "Creates items.",
        "components": [
            {"name": "Items API", "role": "api", "icon": "server",
             "description": "Takes new items.", "modules": ["backend/app/api/routes"]},
            {"name": "Items service", "role": "service", "icon": "cog",
             "description": "Saves them.", "modules": ["backend/app/services"]},
            {"name": "Settings", "role": "other", "icon": "wrench",
             "description": "Its values.", "modules": ["backend/app/core"]},
        ],
    }  # fmt: skip

    def reply(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=message(json.dumps(answer)))

    api_app.state.ask_client = FakeAnthropic(routes={"/v1/messages": reply}).client()
    made = await client.post(f"{url}/diagrams", json={"whole_app": True}, headers=headers)
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["source"], body["kind"], body["prompt"]) == (
        "repo", "architecture", {"whole_app": True},
    )  # fmt: skip
    assert body["title"] == "Architecture of o/items"
    assert len(sent) == 1
    assert "backend/app/api/routes [api]" in sent[0]["messages"][0]["content"]
    spec = body["spec"]
    assert spec["summary"] == "Creates items."
    nodes = {n["label"]: n for n in spec["nodes"]}
    assert {"Items API", "Items service", "Client"} <= set(nodes)
    assert (nodes["Items API"]["icon"], nodes["Items API"]["note"]) == (
        "server",
        "Takes new items.",
    )
    lines = {(e["from"], e["to"]): e for e in spec["edges"]}
    api, service = nodes["Items API"]["id"], nodes["Items service"]["id"]
    assert lines[(api, service)]["verified"] is True  # the graph's call
    client_line = lines[("actor:user", api)]
    assert (client_line["verified"], client_line["inferred"]["reason"]) == (
        False, "clients call its endpoints over HTTP",
    )  # fmt: skip
    # Its use is in the ledger, as a diagram's.
    events = (await db_session.scalars(select(UsageEvent))).all()
    assert [e.ref_type for e in events] == ["diagram"]
    both = await client.post(
        f"{url}/diagrams", json={"whole_app": True, "question": "items"}, headers=headers
    )
    assert (both.status_code, both.json()["error"]["code"]) == (422, "diagram_source")


def _schema_node(snapshot: Snapshot, kind: str, key: str, name: str, **attrs: Any) -> CodeNode:
    return CodeNode(
        org_id=snapshot.org_id, repo_id=snapshot.repo_id, snapshot_id=snapshot.id, kind=kind,
        symbol_key=key, name=name, qualified_name=name, file_id=None, start_line=1, end_line=1,
        start_col=0, end_col=0, lang="sql", attrs=attrs, content_hash=b"\0" * 32,
    )  # fmt: skip


@pytest.mark.integration
async def test_the_whole_schema_is_an_er_diagram_from_the_graph_alone(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    url, snapshot, headers = await _indexed(api_app, client, db_session, tmp_path)
    user, item = "table user", "table item"
    db_session.add_all([
        _schema_node(snapshot, "table", user, "user", sources=["model"]),
        _schema_node(snapshot, "table", item, "item", sources=["model"]),
        _schema_node(snapshot, "table", "table raw", "raw", sources=["sql"]),  # SQL names it only
        _schema_node(
            snapshot, "column", "column user.id", "id", table=user, type="UUID", pk=True,
            nullable=False,
        ),
        _schema_node(snapshot, "column", "column item.id", "id", table=item, type="UUID", pk=True),
        _schema_node(
            snapshot, "column", "column item.owner_id", "owner_id", table=item, type="UUID",
            nullable=False, references=user, references_column="id",
        ),
    ])  # fmt: skip
    await db_session.flush()
    asked: list[object] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return httpx2.Response(500)

    api_app.state.ask_client = FakeAnthropic(routes={"/v1/messages": reply}).client()
    made = await client.post(f"{url}/diagrams", json={"whole_schema": True}, headers=headers)
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["source"], body["kind"], body["prompt"], body["title"]) == (
        "repo", "er", {"whole_schema": True}, "Schema of o/items",
    )  # fmt: skip
    assert asked == []  # no model
    nodes = {n["id"]: n for n in body["spec"]["nodes"]}
    assert set(nodes) == {user, item}  # a table only SQL names, with no columns, is left out
    assert nodes[item]["members"][1] == {
        "name": "owner_id", "kind": "fk", "type": "UUID", "ref": "user.id", "nullable": False,
    }  # fmt: skip
    [edge] = body["spec"]["edges"]
    assert (edge["from"], edge["to"], edge["type"], edge["verified"]) == (
        item, user, "many_to_one", True,
    )  # fmt: skip
    assert body["mermaid"].startswith("erDiagram")
    # An ER diagram of a question: what its code reaches (none of these tables: nothing).
    asked_er = await client.post(
        f"{url}/diagrams", json={"question": "create item", "kind": "er"}, headers=headers
    )
    assert asked_er.status_code == 404, asked_er.text
    assert asked_er.json()["error"]["code"] == "diagram_nothing_found"
    assert asked == []
