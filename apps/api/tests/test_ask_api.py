"""Ask (4.7) through the API: a question streamed as the UI Message Stream, its conversation
stored (messages, parts, confidence, title), followed up, picked up again, listed, deleted."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import IndexJob, JobKind, JobStatus, Repo, RepoSource, Snapshot
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.orgs.service import OrgService
from sensei_engine.agent.tools import ToolContext
from sensei_testing import sign_in
from sensei_testing.anthropic import FakeAnthropic, message, sse

pytestmark = pytest.mark.integration

API = "/api/v1"
SHA = "c" * 40
ANSWER = "Items are saved by create_item."


def _streamed(text: str) -> dict[str, Any]:
    found = message("", stop_reason="end_turn")
    found["content"] = [{"type": "text", "text": text}]
    return found


class _Model:
    """Answers in words (it reads nothing), checks the answer whole, titles the question."""

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            self.turns.append(body)
            return sse(_streamed(ANSWER))
        system = body["system"][0]["text"]
        if system.startswith("Title this question"):
            return httpx2.Response(200, json=message("Saving items"))
        whole: dict[str, Any] = {"complete": True, "missing": [], "gap": ""}
        return httpx2.Response(200, json=message(json.dumps(whole)))


@pytest.fixture
def model() -> _Model:
    return _Model()


@pytest.fixture
def app(
    api_app: FastAPI,
    fake_redis: FakeAsyncRedis,
    db_session: AsyncSession,
    model: _Model,
    tmp_path: Path,
) -> FastAPI:
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    api_app.state.ask_client = FakeAnthropic(routes={"/v1/messages": model}).client()

    @asynccontextmanager
    async def same_session() -> AsyncGenerator[AsyncSession]:
        yield db_session  # the run's own session sees the test's (rolled back) data

    async def files(_: Request, db: AsyncSession, repo: Repo, snapshot: Snapshot) -> ToolContext:
        (tmp_path / "README.md").write_text("# Items\n")
        return ToolContext(
            db=db,
            org_id=repo.org_id,
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            repo="o/items",
            sha=snapshot.commit_sha,
            root=tmp_path,
            tree=["README.md"],
        )

    api_app.state.stream_sessions = same_session
    api_app.state.ask_context = files
    return api_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as outside:
        app.state.http = outside
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def _setup(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, *, indexed: bool = True
) -> tuple[str, dict[str, str]]:
    user, headers = await sign_in(app, client, db)
    access = await app.state.org_service.create(
        db, user, name="Acme", slug=f"acme-{user.id.hex[:8]}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=31,
        owner="o",
        name="items",
        full_name="o/items",
        slug="items",
        default_branch="main",
        visibility="public",
        last_indexed_sha=SHA,
    )
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(
        org_id=repo.org_id,
        repo_id=repo.id,
        ref="main",
        commit_sha=SHA,
        status=SnapshotStatus.READY,
        ready_at=datetime.now(UTC),
        merkle_root="00" * 32,
        symbol_count=1 if indexed else None,
        chunk_count=1 if indexed else None,
    )
    db.add(snapshot)
    await db.flush()
    db.add(
        IndexJob(
            org_id=repo.org_id,
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            kind=JobKind.FULL,
            status=JobStatus.SUCCEEDED,
        )
    )
    await db.flush()
    return f"{API}/orgs/{access.org.slug}", headers


def _events(body: str) -> list[tuple[str | None, str]]:
    """The stream's events: (their id, their data)."""
    found: list[tuple[str | None, str]] = []
    for chunk in body.replace("\r\n", "\n").split("\n\n"):
        lines = [line for line in chunk.split("\n") if line and not line.startswith(":")]
        data = [line.removeprefix("data: ") for line in lines if line.startswith("data: ")]
        ids = [line.removeprefix("id: ") for line in lines if line.startswith("id: ")]
        if data:
            found.append((ids[0] if ids else None, "\n".join(data)))
    return found


async def _ask(
    app: FastAPI, client: httpx.AsyncClient, org: str, headers: dict[str, str], **body: Any
) -> tuple[httpx.Response, list[tuple[str | None, str]]]:
    response = await client.post(f"{org}/repos/items/ask", json=body, headers=headers)
    await asyncio.gather(*list(getattr(app.state, "ask_runs", ())))  # the run, stored
    return response, _events(response.text)


async def test_a_question_streams_its_answer_and_is_stored(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, model: _Model
) -> None:
    org, headers = await _setup(app, client, db_session)
    response, events = await _ask(app, client, org, headers, message="Where are items saved?")
    assert response.status_code == 200, response.text
    assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    parts = [json.loads(data) for _, data in events[:-1]]
    assert events[-1] == (None, "[DONE]")
    assert parts[0]["type"] == "start"
    types = [p["type"] for p in parts]
    assert types[-1] == "finish"
    assert "data-confidence" in types
    # Its research answered in words, reminded once to commit evidence: its words are the answer.
    texts = "".join(p["delta"] for p in parts if p["type"] in ("text-delta", "reasoning-delta"))
    assert ANSWER in texts

    conversation_id = response.headers["x-conversation-id"]
    stored = (await client.get(f"{org}/conversations/{conversation_id}")).json()
    assert stored["title"] == "Saving items"
    user, assistant = stored["messages"]
    assert (user["role"], user["parts"]) == (
        "user",
        [{"type": "text", "text": "Where are items saved?"}],
    )
    assert (assistant["role"], assistant["status"]) == ("assistant", "done")
    assert assistant["id"] == parts[0]["messageId"]
    assert assistant["confidence"]["level"] in ("high", "medium", "low")
    kinds = [p["type"] for p in assistant["parts"]]
    assert "data-confidence" in kinds
    assert "data-searchlog" in kinds

    listed = (await client.get(f"{org}/repos/items/conversations")).json()
    assert [c["id"] for c in listed["conversations"]] == [conversation_id]

    # A follow-up reads the exchange before it.
    model.turns.clear()
    again, _ = await _ask(
        app, client, org, headers, message="And deleted?", conversation_id=conversation_id
    )
    assert again.headers["x-conversation-id"] == conversation_id
    assert model.turns[0]["messages"][:2] == [
        {"role": "user", "content": "Where are items saved?"},
        {"role": "assistant", "content": ANSWER},
    ]


async def test_a_stream_is_picked_up_where_the_client_left_it(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session)
    _, events = await _ask(app, client, org, headers, message="Where are items saved?")
    first_id = events[0][0]
    assert first_id is not None
    message_id = json.loads(events[0][1])["messageId"]
    resumed = await client.get(
        f"{org}/repos/items/ask/messages/{message_id}/stream", headers={"Last-Event-ID": first_id}
    )
    assert resumed.status_code == 200, resumed.text
    again = _events(resumed.text)
    assert [data for _, data in again] == [data for _, data in events[1:]]


async def test_a_conversation_is_its_owners_and_goes_when_deleted(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session)
    response, _ = await _ask(app, client, org, headers, message="Where are items saved?")
    conversation_id = response.headers["x-conversation-id"]
    gone = await client.delete(f"{org}/conversations/{conversation_id}", headers=headers)
    assert gone.status_code == 204
    missing = await client.get(f"{org}/conversations/{conversation_id}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "conversation_not_found"


async def test_a_repository_not_indexed_for_questions_says_so(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session, indexed=False)
    response = await client.post(
        f"{org}/repos/items/ask", json={"message": "Where are items saved?"}, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_indexed"
