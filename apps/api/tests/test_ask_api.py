"""Ask (4.7) through the API: a question streamed as the UI Message Stream, its conversation
stored (messages, parts, confidence, title), followed up, picked up again, listed, deleted."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.answer_cache import lookup, question_key
from sensei_api.routers.ask import stop_runs
from sensei_api.spend import HOLDS_PREFIX, held, hold, release
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AgentRun,
    AgentStep,
    AnswerCacheEntry,
    AuditEvent,
    Conversation,
    ConversationMessage,
    FeatureMap,
    IndexJob,
    JobKind,
    JobStatus,
    Membership,
    MessageFeedback,
    Repo,
    RepoFile,
    RepoSource,
    Snapshot,
    UsageEvent,
    UsageKind,
    User,
)
from sensei_core.models.indexing import SnapshotStatus
from sensei_core.orgs.service import OrgService
from sensei_core.repos.access import RepoAccess
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
    assert parts[1] == {
        "type": "data-conversation",
        "id": "conversation",
        "data": {
            "id": response.headers["x-conversation-id"],
            "runId": response.headers["x-agent-run-id"],
        },
    }
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
    assert assistant["run_id"] == response.headers["x-agent-run-id"]
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


# ---------------------------------------------------------------- runs (4.8)


def _agent_settings(app: FastAPI, **changes: Any) -> None:
    """The app's agent settings changed for one test (a copy: the settings are the session's)."""
    settings = app.state.settings
    agent = settings.agent.model_copy(update=changes)
    app.state.settings = settings.model_copy(update={"agent": agent})


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(
        self, bucket: str, key: str, data: bytes, *, content_type: str | None = None
    ) -> None:
        self.objects[f"{bucket}/{key}"] = data


async def test_a_run_is_recorded_step_by_step(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, model: _Model
) -> None:
    org, headers = await _setup(app, client, db_session)
    listing: dict[str, Any] = {"type": "tool_use", "id": "t1", "name": "list_dir", "input": {}}
    called = message("", stop_reason="tool_use")
    called["content"] = [listing]
    replies = [sse(called)]
    original = model.__call__

    def answer(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream") and replies:
            model.turns.append(body)
            return replies.pop(0)
        return original(request)

    app.state.ask_client = FakeAnthropic(routes={"/v1/messages": answer}).client()
    storage = _Storage()
    app.state.storage = storage
    _agent_settings(app, inline_output_bytes=10)  # a listing is more: to the store
    response, _ = await _ask(app, client, org, headers, message="Where are items saved?")
    run_id = response.headers["x-agent-run-id"]

    detail = (await client.get(f"{org}/agent-runs/{run_id}")).json()
    assert (detail["status"], detail["kind"], detail["question"]) == (
        "succeeded", "ask", "Where are items saved?",
    )  # fmt: skip
    assert detail["tokens_in"] > 0
    assert float(detail["cost_usd"]) > 0
    assert detail["latency_ms"] is not None
    kinds = [(s["kind"], s["name"]) for s in detail["steps"]]
    assert kinds[0] == ("llm", "claude-sonnet-5-5")
    assert ("tool", "list_dir") in kinds
    assert ("verify", "verify") in kinds
    assert [s["idx"] for s in detail["steps"]] == sorted(s["idx"] for s in detail["steps"])
    assert all(s["offset_ms"] >= 0 for s in detail["steps"])
    tool = next(s for s in detail["steps"] if s["kind"] == "tool")
    assert tool["has_output"]
    assert tool["output"] is None  # too big for its row: in the store
    (stored,) = storage.objects
    assert stored.endswith(f"/runs/{run_id}/{tool['idx']}.json")

    listed = (await client.get(f"{org}/repos/items/runs")).json()
    assert [r["id"] for r in listed["runs"]] == [run_id]
    assert listed["runs"][0]["user"] is not None

    replayed = await client.get(f"{org}/agent-runs/{run_id}/events")
    assert replayed.status_code == 200
    data = [json.loads(d) for _, d in _events(replayed.text)]
    assert [d["idx"] for d in data if d["type"] == "step"] == [s["idx"] for s in detail["steps"]]
    assert data[-1] == {"type": "finished", "status": "succeeded"}


async def test_asking_too_often_or_past_the_days_budget_is_refused(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session)
    _agent_settings(app, ask_per_minute=1)
    first, _ = await _ask(app, client, org, headers, message="Where are items saved?")
    assert first.status_code == 200
    second = await client.post(
        f"{org}/repos/items/ask", json={"message": "And deleted?"}, headers=headers
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert int(second.headers["retry-after"]) >= 1


async def test_an_org_past_its_daily_cap_starts_no_run(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session)
    repo = await db_session.scalar(select(Repo).where(Repo.slug == "items"))
    assert repo is not None
    _agent_settings(app, daily_cost_cap_usd=Decimal("1"))

    async def spend(amount: str) -> None:
        db_session.add(
            UsageEvent(
                org_id=repo.org_id,
                kind=UsageKind.LLM,
                provider="anthropic",
                model="claude-sonnet-5-5",
                cost_usd=Decimal(amount),
                price_version="test",
            )
        )
        await db_session.flush()

    await spend("0.85")  # past the warning's share, under the cap: asked, and told
    warned, _ = await _ask(app, client, org, headers, message="Where are items saved?")
    assert warned.status_code == 200
    told = await db_session.scalar(
        select(func.count()).where(AuditEvent.action == "agent.cost_cap_warning")
    )
    assert told == 1
    await spend("1")
    refused = await client.post(
        f"{org}/repos/items/ask", json={"message": "And deleted?"}, headers=headers
    )
    assert refused.status_code == 429
    error = refused.json()["error"]
    assert error["code"] == "cost_cap_reached"
    assert datetime.fromisoformat(error["details"]["resets_at"]) > datetime.now(UTC)


# ---------------------------------------------------------------- the answer cache (4.9)


def _cited_run() -> list[dict[str, Any]]:
    """A run that reads the README, commits to it, and answers citing it."""
    read = message("", stop_reason="tool_use")
    read["content"] = [
        {"type": "tool_use", "id": "t1", "name": "read_code", "input": {"path": "README.md"}}
    ]
    submit = message("", stop_reason="tool_use")
    candidates = [
        {"target": "README.md", "start_line": 1, "end_line": 1, "layer": "other", "why": "it"}
    ]
    submit["content"] = [
        {
            "type": "tool_use",
            "id": "t2",
            "name": "submit_evidence",
            "input": {"candidates": candidates},
        }
    ]
    answered = message("", stop_reason="end_turn")
    answered["content"] = [
        {
            "type": "text",
            "text": "The README names the items.",
            "citations": [
                {
                    "type": "search_result_location",
                    "source": "s",
                    "title": "t",
                    "cited_text": "c",
                    "search_result_index": 0,
                    "start_block_index": 0,
                    "end_block_index": 1,
                }
            ],
        }
    ]
    return [read, submit, answered]


def _answers_citing(app: FastAPI) -> list[dict[str, Any]]:
    """The model answers once, citing the README (:func:`_cited_run`); its streamed turns."""
    turns = _cited_run()
    streamed: list[dict[str, Any]] = []

    def model(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            streamed.append(body)
            return sse(turns.pop(0))
        system = body["system"][0]["text"]
        if system.startswith("Title this question"):
            return httpx2.Response(200, json=message("The README"))
        if "against the code it cites" in system:
            verdicts = {"claims": [{"n": 1, "verdict": "supported"}]}
            return httpx2.Response(200, json=message(json.dumps(verdicts)))
        whole: dict[str, Any] = {"complete": True, "missing": [], "gap": ""}
        return httpx2.Response(200, json=message(json.dumps(whole)))

    app.state.ask_client = FakeAnthropic(routes={"/v1/messages": model}).client()
    return streamed


async def test_the_same_question_again_is_answered_from_the_cache(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    streamed = _answers_citing(app)
    org, headers = await _setup(app, client, db_session)
    first, _ = await _ask(app, client, org, headers, message="What does the README say?")
    assert first.status_code == 200
    assert "x-answer-cache" not in first.headers
    asked = len(streamed)

    # Asked first in another conversation (a follow-up's words mean their conversation's).
    again, events = await _ask(
        app,
        client,
        org,
        headers,
        message="what does the readme say",  # the same words, but case and the question mark
    )
    conversation_id = again.headers["x-conversation-id"]
    assert again.headers["x-answer-cache"] == "hit"
    assert len(streamed) == asked  # no new run
    parts = [json.loads(data) for _, data in events[:-1]]
    assert parts[0]["type"] == "start"
    assert "".join(p["delta"] for p in parts if p["type"] == "text-delta") == (
        "The README names the items."
    )
    assert {"type": "data-cache"}.items() <= next(
        p for p in parts if p["type"] == "data-cache"
    ).items()
    stored = (await client.get(f"{org}/conversations/{conversation_id}")).json()
    served = stored["messages"][-1]
    assert served["status"] == "done"
    assert [c["provenance"] for c in served["citations"]] == ["cache"]
    assert served["citations"][0]["path"] == "README.md"


async def test_a_kept_answer_whose_file_changed_is_not_served(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    turns = _cited_run()

    def model(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            return sse(turns.pop(0))
        system = body["system"][0]["text"]
        if "against the code it cites" in system:
            verdicts = {"claims": [{"n": 1, "verdict": "supported"}]}
            return httpx2.Response(200, json=message(json.dumps(verdicts)))
        whole: dict[str, Any] = {"complete": True, "missing": [], "gap": ""}
        return httpx2.Response(200, json=message(json.dumps(whole)))

    app.state.ask_client = FakeAnthropic(routes={"/v1/messages": model}).client()
    org, headers = await _setup(app, client, db_session)
    await _ask(app, client, org, headers, message="What does the README say?")
    entry = await db_session.scalar(select(AnswerCacheEntry))
    assert entry is not None
    asked = "What does the README say?"

    async def found() -> AnswerCacheEntry | None:
        return await lookup(
            db_session, snapshot_id=entry.snapshot_id, mode="fast", question=asked, embedding=None
        )

    assert await found() == entry
    entry.file_hashes = {"README.md": "ff" * 32}  # as if the file had changed since
    await db_session.flush()
    assert await found() is None
    assert question_key("What does the README say?") == question_key("  what does the readme say ")


async def test_a_repositorys_feature_maps_are_listed_by_path(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, _ = await _setup(app, client, db_session)
    snapshot = await db_session.scalar(select(Snapshot).where(Snapshot.commit_sha == SHA))
    assert snapshot is not None
    for path in ("web", "api"):
        db_session.add(
            FeatureMap(
                org_id=snapshot.org_id,
                repo_id=snapshot.repo_id,
                snapshot_id=snapshot.id,
                key=f"dir:{path}",
                path=path,
                title=f"The {path}",
                summary="What it does.",
                entry_points=[],
                key_symbols=[],
                data_flow=["In.", "Out."],
                citations=[],
            )
        )
    await db_session.flush()
    listed = (await client.get(f"{org}/repos/items/feature-maps")).json()
    assert listed["sha"] == SHA
    assert [(m["path"], m["title"], m["stale"]) for m in listed["maps"]] == [
        ("api", "The api", False), ("web", "The web", False),
    ]  # fmt: skip


# ---------------------------------------------------------------- feedback, history, sharing (4.11)


async def test_feedback_on_an_answer_is_one_per_user_and_replaced(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session)
    response, _ = await _ask(app, client, org, headers, message="Where are items saved?")
    conversation_id = response.headers["x-conversation-id"]
    question, answer = (await client.get(f"{org}/conversations/{conversation_id}")).json()[
        "messages"
    ]
    url = f"{org}/messages/{answer['id']}/feedback"

    said = await client.post(url, json={"kind": "helpful"}, headers=headers)
    assert said.status_code == 200, said.text
    assert said.json()["kind"] == "helpful"
    again = await client.post(
        url, json={"kind": "wrong_file", "note": "  it's in services  "}, headers=headers
    )
    assert (again.json()["kind"], again.json()["note"]) == ("wrong_file", "it's in services")
    assert await db_session.scalar(select(func.count()).select_from(MessageFeedback)) == 1
    stored = (await client.get(f"{org}/conversations/{conversation_id}")).json()["messages"]
    assert stored[0]["feedback"] is None
    assert stored[1]["feedback"]["kind"] == "wrong_file"

    unknown = await client.post(url, json={"kind": "rude"}, headers=headers)
    assert unknown.status_code == 422
    on_a_question = await client.post(
        f"{org}/messages/{question['id']}/feedback", json={"kind": "helpful"}, headers=headers
    )
    assert on_a_question.status_code == 404
    assert on_a_question.json()["error"]["code"] == "message_not_found"

    taken_back = await client.delete(url, headers=headers)
    assert taken_back.status_code == 204
    stored = (await client.get(f"{org}/conversations/{conversation_id}")).json()["messages"]
    assert stored[1]["feedback"] is None


async def test_a_conversation_is_renamed_and_found_by_its_title(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session)
    response, _ = await _ask(app, client, org, headers, message="Where are items saved?")
    conversation_id = response.headers["x-conversation-id"]

    renamed = await client.patch(
        f"{org}/conversations/{conversation_id}",
        json={"title": "  Saving   items_100%  "},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Saving items_100%"
    blank = await client.patch(
        f"{org}/conversations/{conversation_id}", json={"title": "   "}, headers=headers
    )
    assert blank.status_code == 422

    async def found(q: str) -> list[str]:
        listed = await client.get(f"{org}/repos/items/conversations", params={"q": q})
        return [c["id"] for c in listed.json()["conversations"]]

    assert await found("ITEMS") == [conversation_id]
    assert await found("items_1") == [conversation_id]
    assert await found("deleting") == []
    assert await found("s%i") == []  # its wildcards are words
    assert await found("_") == [conversation_id]
    assert await found("x_x") == []


async def test_a_shared_conversation_hides_its_citations_from_who_cant_read_the_repo(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answers_citing(app)
    org, headers = await _setup(app, client, db_session)
    response, _ = await _ask(app, client, org, headers, message="What does the README say?")
    conversation_id = response.headers["x-conversation-id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    owner = await db_session.get(User, conversation.user_id)
    assert owner is not None
    repo = await db_session.get(Repo, conversation.repo_id)
    assert repo is not None
    repo.source = RepoSource.GITHUB_APP  # read only by who GitHub lets read it: the owner
    await db_session.flush()

    async def readable(_: RepoAccess, user: User, repos: list[Repo]) -> list[Repo]:
        return repos if user.id == owner.id else []

    monkeypatch.setattr(RepoAccess, "readable", readable)

    first = await client.post(f"{org}/conversations/{conversation_id}/share", headers=headers)
    assert first.status_code == 200, first.text
    shared = await client.post(f"{org}/conversations/{conversation_id}/share", headers=headers)
    token = shared.json()["token"]
    assert shared.json()["path"] == f"/s/{token}"
    listed = (await client.get(f"{org}/repos/items/conversations")).json()["conversations"]
    assert listed[0]["shared"] is True
    actions = await db_session.scalars(
        select(AuditEvent.action).where(AuditEvent.target_id == conversation_id)
    )
    assert list(actions) == ["conversation.shared", "conversation.shared"]

    # The owner, who can read the repository: all of it, but for the run behind it.
    seen = (await client.get(f"{API}/s/{token}")).json()
    assert (seen["redacted"], seen["repo"], seen["title"]) == (False, "o/items", "The README")
    answer = seen["messages"][-1]
    assert [c["path"] for c in answer["citations"]] == ["README.md"]
    kinds = {p["type"] for p in answer["parts"]}
    assert "tool-read_code" in kinds
    assert "data-conversation" not in kinds
    assert answer["feedback"] is None
    assert answer["run_id"] is None  # the run behind it is the org's

    # Anyone else: the words, and of each citation only its number and place in the text.
    for viewer in ("anonymous", "another user"):
        client.cookies.clear()
        if viewer == "another user":
            await sign_in(app, client, db_session)
        _assert_redacted((await client.get(f"{API}/s/{token}")).json())

    # The first link was replaced; unsharing ends the second.
    assert (await client.get(f"{API}/s/{first.json()['token']}")).status_code == 404
    _, headers = await sign_in(app, client, db_session, owner)
    gone = await client.delete(f"{org}/conversations/{conversation_id}/share", headers=headers)
    assert gone.status_code == 204
    client.cookies.clear()
    missing = await client.get(f"{API}/s/{token}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "share_not_found"


def _assert_redacted(seen: dict[str, Any]) -> None:
    assert (seen["redacted"], seen["repo"]) == (True, None)
    answer = seen["messages"][-1]
    assert answer["citations"] == []
    kinds = {p["type"] for p in answer["parts"]}
    assert kinds <= {"text", "data-confidence", "data-citation", "data-abstain"}
    cited = [p["data"] for p in answer["parts"] if p["type"] == "data-citation"]
    assert cited
    assert all(set(c) == {"n", "span", "redacted"} for c in cited)
    assert "README.md" not in json.dumps(answer["parts"])


# ---------------------------------------------------------------- spend and failures (audit 4.11)


def _savepoints(app: FastAPI, db: AsyncSession) -> None:
    """Each of a run's sessions in a savepoint of the test's, rolled back on an error as the
    real sessions' transactions are."""

    @asynccontextmanager
    async def savepoint() -> AsyncGenerator[AsyncSession]:
        async with db.begin_nested():
            yield db

    app.state.stream_sessions = savepoint


async def test_a_failed_run_keeps_what_it_spent_and_its_steps(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, model: _Model
) -> None:
    org, headers = await _setup(app, client, db_session)
    _savepoints(app, db_session)
    called = message("", stop_reason="tool_use")
    called["content"] = [{"type": "tool_use", "id": "t1", "name": "list_dir", "input": {}}]
    replies = [sse(called)]

    def failing(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            if replies:
                return replies.pop(0)
            refused = {"type": "invalid_request_error", "message": "no"}
            return httpx2.Response(400, json={"type": "error", "error": refused})
        return model(request)

    app.state.ask_client = FakeAnthropic(routes={"/v1/messages": failing}).client()
    response, events = await _ask(app, client, org, headers, message="Where are items saved?")
    run_id = response.headers["x-agent-run-id"]
    assert any(json.loads(d)["type"] == "error" for _, d in events if d != "[DONE]")

    spent = await db_session.scalar(
        select(func.count()).select_from(UsageEvent).where(UsageEvent.ref_id == run_id)
    )
    assert spent == 1  # the turn that ran, though the run failed after it
    record = await db_session.get(AgentRun, uuid.UUID(run_id))
    assert record is not None
    assert (record.status, record.error) == ("failed", "LLMError")
    names = await db_session.scalars(select(AgentStep.name).where(AgentStep.run_id == record.id))
    assert "list_dir" in list(names)
    answer = await db_session.scalar(
        select(ConversationMessage).where(ConversationMessage.run_id == record.id)
    )
    assert answer is not None
    assert answer.status == "failed"


async def test_a_run_stopped_at_shutdown_says_so(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session)
    reading = asyncio.Event()

    async def stuck(*_: Any) -> ToolContext:
        reading.set()
        await asyncio.Event().wait()  # never: until the run is stopped
        raise AssertionError

    app.state.ask_context = stuck
    asked = asyncio.create_task(
        client.post(f"{org}/repos/items/ask", json={"message": "Where?"}, headers=headers)
    )
    await asyncio.wait_for(reading.wait(), timeout=5)
    await stop_runs(app.state)
    response = await asyncio.wait_for(asked, timeout=5)
    assert '"type":"error"' in response.text
    answer = await db_session.scalar(
        select(ConversationMessage).where(
            ConversationMessage.id
            == uuid.UUID(json.loads(_events(response.text)[0][1])["messageId"])
        )
    )
    assert answer is not None
    assert answer.status == "failed"
    record = await db_session.get(AgentRun, answer.run_id)
    assert record is not None
    assert (record.status, record.error) == ("failed", "CancelledError")


async def test_runs_under_way_hold_their_budget_from_the_day(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    org, headers = await _setup(app, client, db_session)
    repo = await db_session.scalar(select(Repo).where(Repo.slug == "items"))
    assert repo is not None
    _agent_settings(app, daily_cost_cap_usd=Decimal("1"))
    other = uuid.uuid4()
    await hold(fake_redis, repo.org_id, other, Decimal("1"))  # another run, under way
    assert await held(fake_redis, repo.org_id) == Decimal("1")
    refused = await client.post(
        f"{org}/repos/items/ask", json={"message": "Where?"}, headers=headers
    )
    assert refused.json()["error"]["code"] == "cost_cap_reached"

    await release(fake_redis, repo.org_id, other)
    lapsed = f"{Decimal('1')} {time.time() - 1}"
    holds = f"{HOLDS_PREFIX}{repo.org_id}"
    await cast("Awaitable[int]", fake_redis.hset(holds, str(other), lapsed))  # pyright: ignore[reportUnknownMemberType]
    asked, _ = await _ask(app, client, org, headers, message="Where are items saved?")
    assert asked.status_code == 200  # a lapsed hold is forgotten
    assert await held(fake_redis, repo.org_id) == Decimal(0)  # and the run's, released


async def test_a_follow_up_is_neither_kept_nor_served_from_the_cache(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    turns: list[dict[str, Any]] = []

    def model(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            if not turns:
                turns.extend(_cited_run())
            return sse(turns.pop(0))
        system = body["system"][0]["text"]
        if system.startswith("Title this question"):
            return httpx2.Response(200, json=message("The README"))
        if "against the code it cites" in system:
            verdicts = {"claims": [{"n": 1, "verdict": "supported"}]}
            return httpx2.Response(200, json=message(json.dumps(verdicts)))
        whole: dict[str, Any] = {"complete": True, "missing": [], "gap": ""}
        return httpx2.Response(200, json=message(json.dumps(whole)))

    app.state.ask_client = FakeAnthropic(routes={"/v1/messages": model}).client()
    org, headers = await _setup(app, client, db_session)
    first, _ = await _ask(app, client, org, headers, message="What does the README say?")
    conversation_id = first.headers["x-conversation-id"]
    follow_up = "And the second one?"
    await _ask(app, client, org, headers, message=follow_up, conversation_id=conversation_id)
    kept = await db_session.scalars(select(AnswerCacheEntry.question))
    assert list(kept) == ["What does the README say?"]  # the first question's alone

    # The follow-up's words asked first elsewhere: a run of their own, not the other's answer.
    again, _ = await _ask(app, client, org, headers, message=follow_up)
    assert "x-answer-cache" not in again.headers


async def test_a_share_link_stops_when_its_sharer_leaves_the_org(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org, headers = await _setup(app, client, db_session)
    response, _ = await _ask(app, client, org, headers, message="Where are items saved?")
    conversation_id = response.headers["x-conversation-id"]
    shared = await client.post(f"{org}/conversations/{conversation_id}/share", headers=headers)
    token = shared.json()["token"]
    assert (await client.get(f"{API}/s/{token}")).status_code == 200
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    await db_session.execute(
        delete(Membership).where(
            Membership.user_id == conversation.user_id, Membership.org_id == conversation.org_id
        )
    )
    gone = await client.get(f"{API}/s/{token}")
    assert gone.status_code == 404


async def test_a_scoped_question_tells_the_agent_where_the_user_was(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, model: _Model
) -> None:
    org, headers = await _setup(app, client, db_session)
    snapshot = await db_session.scalar(select(Snapshot).where(Snapshot.commit_sha == SHA))
    assert snapshot is not None
    db_session.add(
        RepoFile(
            org_id=snapshot.org_id,
            repo_id=snapshot.repo_id,
            snapshot_id=snapshot.id,
            path="README.md",
            blob_sha="a" * 40,
            content_hash=b"\x00" * 32,
            size=8,
            language="markdown",
            role="source",
        )
    )
    await db_session.flush()
    scope = {"path": "README.md", "start_line": 1, "end_line": 1}
    response, _ = await _ask(app, client, org, headers, message="What is this?", scope=scope)
    assert response.status_code == 200, response.text
    asked = model.turns[0]["messages"][-1]["content"]
    assert "What is this?" in str(asked)
    assert "(Asked while looking at README.md line 1.)" in str(asked)
    stored = await client.get(f"{org}/conversations/{response.headers['x-conversation-id']}")
    question = stored.json()["messages"][0]
    assert question["parts"] == [
        {"type": "text", "text": "What is this?"},
        {
            "type": "data-scope",
            "id": "scope",
            "data": {"path": "README.md", "startLine": 1, "endLine": 1, "layers": []},
        },
    ]
    assert await db_session.scalar(select(func.count()).select_from(AnswerCacheEntry)) == 0

    missing = await client.post(
        f"{org}/repos/items/ask",
        json={"message": "And this?", "scope": {"path": "gone.py"}},
        headers=headers,
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "scope_not_found"


async def test_a_question_scoped_to_a_directory_and_layers_says_so(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, model: _Model
) -> None:
    org, headers = await _setup(app, client, db_session)
    snapshot = await db_session.scalar(select(Snapshot).where(Snapshot.commit_sha == SHA))
    assert snapshot is not None
    db_session.add(
        RepoFile(
            org_id=snapshot.org_id, repo_id=snapshot.repo_id, snapshot_id=snapshot.id,
            path="backend/app/crud.py", blob_sha="b" * 40, content_hash=b"\x01" * 32, size=8,
            language="python", role="service",
        )
    )  # fmt: skip
    await db_session.flush()
    scope = {"path": "backend/app/", "layers": ["svc", "db", "svc"]}
    response, _ = await _ask(app, client, org, headers, message="Where is it saved?", scope=scope)
    assert response.status_code == 200, response.text
    asked = str(model.turns[0]["messages"][-1]["content"])
    assert "(Asked while looking at backend/app.)" in asked
    assert "(Asked about the service and data layers.)" in asked
    stored = await client.get(f"{org}/conversations/{response.headers['x-conversation-id']}")
    (_, part) = stored.json()["messages"][0]["parts"]
    assert part["data"] == {
        "path": "backend/app", "startLine": None, "endLine": None, "layers": ["svc", "db"],
    }  # fmt: skip

    async def refused(body: dict[str, Any]) -> str:
        sent = await client.post(
            f"{org}/repos/items/ask", json={"message": "x", "scope": body}, headers=headers
        )
        assert sent.status_code == 422, sent.text
        return str(sent.json()["error"]["code"])

    assert await refused({"layers": ["ui"], "start_line": 3}) == "validation_failed"
    assert await refused({"path": "backend", "start_line": 2}) == "scope_lines"  # a directory
    assert await refused({"path": "back"}) == "scope_not_found"  # a prefix isn't a directory
    before = len(model.turns)
    only_layers, _ = await _ask(
        app, client, org, headers, message="What does the UI do?", scope={"layers": ["ui"]}
    )
    assert only_layers.status_code == 200
    first = str(model.turns[before]["messages"][-1]["content"])  # the run's first turn
    assert "(Asked about the UI layer.)" in first
    assert "looking at" not in first
