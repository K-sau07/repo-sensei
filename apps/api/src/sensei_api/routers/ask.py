"""Ask (4.7): a question about a repository, answered by the agent and streamed as the AI SDK's
UI Message Stream (protocol v1, :mod:`sensei_api.ui_stream`), and the conversations it builds.

``POST /orgs/{org}/repos/{repo}/ask`` stores the question and an answer being written, then
streams it. The run is the server's, not the connection's: it writes each part to a Redis
stream (``ask:<message id>``, kept an hour) and the response follows that stream, so a client
that goes away loses nothing: the run goes on and stores the answer when it ends, and ``GET
…/ask/messages/{id}/stream`` picks the stream up again (from ``Last-Event-ID``). The run starts
once the response does, after the request's transaction has committed the rows it updates.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Annotated, Any, Literal, cast

import orjson
import structlog
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse, ServerSentEvent

from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoDep
from sensei_api.routers import browse, search
from sensei_api.streams import HEADERS, HEARTBEAT_SECONDS, READ_BLOCK_MS, last_event_id
from sensei_api.ui_stream import DONE, HEADER, UIStream, confidence_data, text_of
from sensei_core.db.tenancy import set_org_context, set_user_context
from sensei_core.errors import ConflictError, InvalidError, NotFoundError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import (
    Conversation,
    ConversationMessage,
    MessageCitation,
    Repo,
    RepoFile,
    Snapshot,
)
from sensei_engine.agent.loop import Answer, AskAgent, Done, Exchange, Mode
from sensei_engine.agent.tools import ToolContext
from sensei_engine.llm.anthropic_client import AnthropicClient, LLMError, LLMRequest, Tier
from sensei_engine.llm.ledger import usage_ledger

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/orgs/{org}", tags=["ask"])

STREAM_PREFIX = "ask:"
STREAM_SECONDS = 3_600  # how long a run's stream may be picked up again
STREAM_MAXLEN = 20_000
MAX_EXCHANGES = 6  # the earlier questions a run reads, newest last
TITLE_CHARS = 80
REF_TYPE = "ask"
FINISHED = frozenset({"finish"})

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]
ContextFactory = Callable[[Request, AsyncSession, Repo, Snapshot], Awaitable[ToolContext]]


# ---------------------------------------------------------------- shapes


class AskRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=4_000)
    mode: Mode = Mode.FAST
    ref: str | None = Field(default=None, max_length=255)


class CitationOut(BaseModel):
    idx: int
    sha: str
    path: str
    start_line: int
    end_line: int
    symbol_key: str | None
    provenance: str
    verified: bool


class MessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    parts: list[dict[str, Any]]
    status: Literal["streaming", "done", "failed"]
    confidence: dict[str, Any] | None
    abstained: bool
    created_at: datetime
    citations: list[CitationOut]


class ConversationOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class ConversationList(BaseModel):
    conversations: list[ConversationOut]
    next_cursor: str | None


# ---------------------------------------------------------------- what a run needs


def _sessions(request: Request) -> Sessions:
    """Where a run gets its session (its own: the request's is gone by then)."""
    custom: Sessions | None = getattr(request.app.state, "stream_sessions", None)
    return custom or request.app.state.database.session


def _client(request: Request) -> AnthropicClient:
    state = request.app.state
    found: AnthropicClient | None = getattr(state, "ask_client", None)
    if found is None:
        found = AnthropicClient(state.settings.anthropic)
        state.ask_client = found
    return found


async def tool_context(
    request: Request, db: AsyncSession, repo: Repo, snapshot: Snapshot
) -> ToolContext:
    """The snapshot as the agent's tools read it: unpacked, its files, its graph and its
    search (the search page's legs)."""
    service = search.get_search_service(request)
    costs = search.cost_calculator(request)
    paths = list(
        await db.scalars(
            select(RepoFile.path).where(RepoFile.snapshot_id == snapshot.id).order_by(RepoFile.path)
        )
    )
    return ToolContext(
        db=db,
        org_id=repo.org_id,
        repo_id=repo.id,
        snapshot_id=snapshot.id,
        repo=f"{repo.owner}/{repo.name}",
        sha=snapshot.commit_sha,
        root=await browse.unpacked(request, snapshot),
        tree=paths,
        searchable=await browse.searchable(request, db, snapshot),
        search=service,
        exact=request.app.state.exact_search,
        graph=await search.search_graph(request, db, snapshot),
        grep_legs=search.SnapshotGrep(request, db, snapshot),
        dense=await search.semantic_legs(
            request, db, request.app.state.redis, snapshot, costs, org_id=repo.org_id
        ),
    )


def _context_factory(request: Request) -> ContextFactory:
    custom: ContextFactory | None = getattr(request.app.state, "ask_context", None)
    return custom or tool_context


def _runs(request: Request) -> set[asyncio.Task[None]]:
    """The process's runs in flight (held, so none is collected before it ends)."""
    state = request.app.state
    held: set[asyncio.Task[None]] | None = getattr(state, "ask_runs", None)
    if held is None:
        held = set()
        state.ask_runs = held
    return held


# ---------------------------------------------------------------- ask


@router.post("/repos/{repo}/ask", response_class=EventSourceResponse)
async def ask(
    *,
    request: Request,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    body: AskRequest,
) -> EventSourceResponse:
    """Ask a question about the repository: its answer, streamed as it is written."""
    snapshot = await resolve_snapshot(db, repo, body.ref)
    if snapshot.symbol_count is None or snapshot.chunk_count is None:
        raise ConflictError("This repository isn't indexed for questions yet.", code="not_indexed")
    user = access.user
    if body.conversation_id is None:
        conversation = Conversation(
            org_id=repo.org_id,
            repo_id=repo.id,
            user_id=user.id,
            title=_title(body.message),
            snapshot_id=snapshot.id,
        )
        db.add(conversation)
        await db.flush()
    else:
        conversation = await _owned(db, body.conversation_id, user.id)
        if conversation.repo_id != repo.id:
            raise NotFoundError("Conversation not found.", code="conversation_not_found")
    earlier = await _exchanges(db, conversation.id)
    question = ConversationMessage(
        org_id=repo.org_id,
        conversation_id=conversation.id,
        role="user",
        parts=[{"type": "text", "text": body.message}],
        snapshot_id=snapshot.id,
    )
    answer = ConversationMessage(
        org_id=repo.org_id,
        conversation_id=conversation.id,
        role="assistant",
        status="streaming",
        snapshot_id=snapshot.id,
    )
    db.add_all([question, answer])
    await db.execute(
        update(Conversation).where(Conversation.id == conversation.id).values(updated_at=func.now())
    )
    await db.flush()
    run = _Run(
        request=request,
        sessions=_sessions(request),
        contexts=_context_factory(request),
        client=_client(request),
        redis=redis,
        org_id=repo.org_id,
        user_id=user.id,
        repo_id=repo.id,
        snapshot_id=snapshot.id,
        conversation_id=conversation.id,
        message_id=answer.id,
        question=body.message,
        mode=body.mode,
        earlier=earlier,
        repo_name=f"{repo.owner}/{repo.name}",
    )
    key = f"{STREAM_PREFIX}{answer.id}"
    headers = {**HEADERS, HEADER[0]: HEADER[1], "x-conversation-id": str(conversation.id)}
    return EventSourceResponse(
        _follow(request, redis, key, "0", start=run.start),
        ping=HEARTBEAT_SECONDS,
        headers=headers,
    )


@router.get("/repos/{repo}/ask/messages/{message_id}/stream", response_class=EventSourceResponse)
async def resume(
    *,
    request: Request,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    message_id: uuid.UUID,
) -> EventSourceResponse:
    """The answer's stream again, after the last event the client saw (``Last-Event-ID``)."""
    message = await db.get(ConversationMessage, message_id)
    if message is None or message.role != "assistant":
        raise NotFoundError("Message not found.", code="message_not_found")
    conversation = await _owned(db, message.conversation_id, access.user.id)
    if conversation.repo_id != repo.id:
        raise NotFoundError("Message not found.", code="message_not_found")
    key = f"{STREAM_PREFIX}{message.id}"
    if not await redis.exists(key):
        raise ConflictError(
            "That answer's stream is over: read it from its conversation.", code="stream_gone"
        )
    headers = {**HEADERS, HEADER[0]: HEADER[1]}
    return EventSourceResponse(
        _follow(request, redis, key, last_event_id(request)),
        ping=HEARTBEAT_SECONDS,
        headers=headers,
    )


# ---------------------------------------------------------------- conversations


@router.get("/repos/{repo}/conversations", response_model=ConversationList)
async def conversations(
    *,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
) -> ConversationList:
    """The signed-in user's conversations about the repository, the latest first."""
    query = select(Conversation).where(
        Conversation.repo_id == repo.id, Conversation.user_id == access.user.id
    )
    if cursor is not None:
        query = query.where(Conversation.updated_at < _cursor_time(cursor))
    rows = list(
        await db.scalars(
            query.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit + 1)
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    after = rows[-1].updated_at.isoformat() if more and rows else None
    return ConversationList(conversations=[_out(c) for c in rows], next_cursor=after)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def conversation(
    *, access: OrgAccessDep, db: DbSession, conversation_id: uuid.UUID
) -> ConversationDetail:
    """A conversation with its messages, each answer with its citations."""
    found = await _owned(db, conversation_id, access.user.id)
    messages = list(
        await db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == found.id)
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
        )
    )
    cited: dict[uuid.UUID, list[CitationOut]] = {}
    if messages:
        rows = await db.scalars(
            select(MessageCitation)
            .where(MessageCitation.message_id.in_([m.id for m in messages]))
            .order_by(MessageCitation.message_id, MessageCitation.idx)
        )
        for row in rows:
            cited.setdefault(row.message_id, []).append(
                CitationOut(
                    idx=row.idx,
                    sha=row.sha,
                    path=row.path,
                    start_line=row.start_line,
                    end_line=row.end_line,
                    symbol_key=row.symbol_key,
                    provenance=row.provenance,
                    verified=row.verified,
                )
            )
    return ConversationDetail(
        **_out(found).model_dump(),
        messages=[
            MessageOut(
                id=m.id,
                role=cast("Literal['user', 'assistant']", m.role),
                parts=m.parts,
                status=cast("Literal['streaming', 'done', 'failed']", m.status),
                confidence=m.confidence,
                abstained=m.abstained,
                created_at=m.created_at,
                citations=cited.get(m.id, []),
            )
            for m in messages
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    *, access: OrgAccessDep, db: DbSession, conversation_id: uuid.UUID
) -> Response:
    """Delete a conversation, its messages and their citations."""
    found = await _owned(db, conversation_id, access.user.id)
    await db.execute(delete(Conversation).where(Conversation.id == found.id))
    return Response(status_code=204)


# ---------------------------------------------------------------- the run


@dataclass(slots=True)
class _Run:
    """One answer being written: started once its response is (:meth:`start`), then on its own,
    whatever becomes of the connection."""

    request: Request
    sessions: Sessions
    contexts: ContextFactory
    client: AnthropicClient
    redis: Redis
    org_id: uuid.UUID
    user_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    question: str
    mode: Mode
    earlier: list[Exchange]
    repo_name: str

    def start(self) -> None:
        task = asyncio.create_task(self._run())
        runs = _runs(self.request)
        runs.add(task)
        task.add_done_callback(runs.discard)

    async def _run(self) -> None:
        key = f"{STREAM_PREFIX}{self.message_id}"
        stream = UIStream(str(self.message_id), self.repo_name)
        await self._publish(key, stream.start())
        answer: Answer | None = None
        try:
            async with self.sessions() as db:
                await set_user_context(db, self.user_id)
                await set_org_context(db, self.org_id)
                repo = await db.get(Repo, self.repo_id)
                snapshot = await db.get(Snapshot, self.snapshot_id)
                if repo is None or snapshot is None:
                    raise LookupError("the repository or its snapshot is gone")
                ctx = await self.contexts(self.request, db, repo, snapshot)
                agent = self._agent(db, ctx)
                async for event in agent.run(
                    self.question, ctx, conversation=self.earlier, mode=self.mode
                ):
                    await self._publish(key, stream.encode(event))
                    if isinstance(event, Done):
                        answer = event.answer
                await self._store(db, stream, answer, ctx.sha)
        except Exception as exc:  # the run's own failure: said in its stream and stored
            log.exception("ask run failed", message_id=str(self.message_id))
            await self._publish(key, stream.failed(_said(exc)))
            await self._failed(stream)

    def _agent(self, db: AsyncSession, ctx: ToolContext) -> AskAgent:
        costs = search.cost_calculator(self.request)
        ledger = usage_ledger(
            db,
            costs,
            org_id=self.org_id,
            user_id=self.user_id,
            ref_type=REF_TYPE,
            ref_id=str(self.message_id),
        )
        return AskAgent(self.client, costs=costs, on_usage=ledger, reranker=ctx.search.reranker)

    async def _publish(self, key: str, parts: list[dict[str, Any]]) -> None:
        for part in parts:
            await self.redis.xadd(  # pyright: ignore[reportUnknownMemberType]
                key,
                {"part": orjson.dumps(part)},
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
        await self.redis.expire(key, STREAM_SECONDS)

    async def _store(
        self, db: AsyncSession, stream: UIStream, answer: Answer | None, sha: str
    ) -> None:
        message = await db.get(ConversationMessage, self.message_id)
        if message is None:  # the conversation was deleted meanwhile
            return
        message.parts = stream.parts
        message.status = "done" if answer is not None else "failed"
        if answer is not None:
            message.confidence = (
                confidence_data(answer.confidence) if answer.confidence is not None else None
            )
            message.abstained = answer.abstained is not None
            verified = _verified(answer)
            for n, citation in enumerate(answer.citations, start=1):
                cited = citation.cited
                db.add(
                    MessageCitation(
                        org_id=self.org_id,
                        message_id=message.id,
                        idx=n,
                        repo_id=self.repo_id,
                        sha=cited.sha or sha,
                        path=cited.path,
                        start_line=cited.start_line,
                        end_line=cited.end_line,
                        symbol_key=cited.symbol_key,
                        provenance="evidence",
                        verified=n in verified,
                    )
                )
        await self._retitle(db)

    async def _failed(self, stream: UIStream) -> None:
        try:
            async with self.sessions() as db:
                await set_user_context(db, self.user_id)
                await set_org_context(db, self.org_id)
                await db.execute(
                    update(ConversationMessage)
                    .where(ConversationMessage.id == self.message_id)
                    .values(status="failed", parts=stream.parts)
                )
        except Exception:
            log.exception("ask run's failure not stored", message_id=str(self.message_id))

    async def _retitle(self, db: AsyncSession) -> None:
        """A first question's conversation titled in a few words (the fast model; its first
        words until then, and if the model can't)."""
        if self.earlier or not self.client.configured:
            return
        request = LLMRequest(
            f"Question: {self.question}",
            system=(
                "Title this question about a code repository in at most six words, as a "
                "conversation list shows it. Answer with the title alone."
            ),
            tier=Tier.FAST,
            max_tokens=64,
            effort="low",
        )
        try:
            titled = await self.client.complete(request)
        except LLMError:
            return
        title = " ".join(titled.text.split()).strip(" \"'.")[:TITLE_CHARS]
        if title:
            await db.execute(
                update(Conversation)
                .where(Conversation.id == self.conversation_id)
                .values(title=title)
            )


# ---------------------------------------------------------------- helpers


async def _follow(
    request: Request,
    redis: Redis,
    key: str,
    cursor: str,
    *,
    start: Callable[[], None] | None = None,
) -> AsyncGenerator[ServerSentEvent]:
    """The stream's parts after ``cursor`` until the answer's ``finish``, then ``[DONE]``; a run
    to start is started first (its response has begun: the request's rows are committed)."""
    if start is not None:
        start()
    while True:
        batches = await cast(
            "Awaitable[list[Any] | None]",
            redis.xread({key: cursor}, count=200, block=READ_BLOCK_MS),  # pyright: ignore[reportUnknownMemberType]
        )
        for _stream, entries in batches or []:
            for entry_id, fields in entries:
                cursor = entry_id
                raw = fields.get(b"part", fields.get("part"))
                part = orjson.loads(raw)
                yield ServerSentEvent(data=orjson.dumps(part).decode(), id=entry_id)
                if part["type"] in FINISHED:
                    yield ServerSentEvent(data=DONE)
                    return
        if await request.is_disconnected():
            return


async def _owned(db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID) -> Conversation:
    found = await db.get(Conversation, conversation_id)
    if found is None or found.user_id != user_id:
        raise NotFoundError("Conversation not found.", code="conversation_not_found")
    return found


async def _exchanges(db: AsyncSession, conversation_id: uuid.UUID) -> list[Exchange]:
    """The conversation's earlier questions and their answers' text, oldest first."""
    rows = list(
        await db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(MAX_EXCHANGES * 2)
        )
    )
    rows.reverse()
    exchanges: list[Exchange] = []
    for asked, answered in pairwise(rows):
        if asked.role == "user" and answered.role == "assistant" and answered.status == "done":
            exchanges.append(Exchange(text_of(asked.parts), text_of(answered.parts)))
    return exchanges[-MAX_EXCHANGES:]


def _verified(answer: Answer) -> set[int]:
    """The citations (by number) that back a claim the support check found supported."""
    check = answer.verification
    if check is None:
        return set()
    supported = {
        cited
        for checked in check.claims
        if checked.verdict == "supported"
        for cited in checked.claim.cited
    }
    return {n for n, c in enumerate(answer.citations, start=1) if c.cited in supported}


def _title(question: str) -> str:
    words = " ".join(question.split())
    return words if len(words) <= TITLE_CHARS else words[: TITLE_CHARS - 1].rstrip() + "…"


def _said(exc: Exception) -> str:
    if isinstance(exc, LLMError) and exc.transient:
        return "The model isn't answering right now; try again in a moment."
    return "The answer couldn't be written."


def _out(found: Conversation) -> ConversationOut:
    return ConversationOut(
        id=found.id,
        repo_id=found.repo_id,
        title=found.title,
        created_at=found.created_at,
        updated_at=found.updated_at,
    )


def _cursor_time(cursor: str) -> datetime:
    try:
        moment = datetime.fromisoformat(cursor)
    except ValueError as exc:
        raise InvalidError("That cursor is no page.", code="invalid_cursor") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
