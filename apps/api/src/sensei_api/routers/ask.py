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
import dataclasses
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
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

from sensei_api import answer_cache
from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.ratelimit import TokenBucket
from sensei_api.repos import RepoDep
from sensei_api.routers import browse, search
from sensei_api.runs import Recorder
from sensei_api.sharing import new_share_token, share_token_hash
from sensei_api.spend import hold, release, remaining_today
from sensei_api.streams import HEADERS, HEARTBEAT_SECONDS, READ_BLOCK_MS, last_event_id
from sensei_api.ui_stream import DONE, HEADER, UIStream, confidence_data, replayed, text_of
from sensei_core.audit import AuditAction, audit
from sensei_core.config import Settings
from sensei_core.db.tenancy import set_org_context, set_user_context
from sensei_core.errors import ConflictError, InvalidError, NotFoundError, RateLimitedError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import (
    AgentRun,
    AnswerCacheEntry,
    Conversation,
    ConversationMessage,
    MessageCitation,
    MessageFeedback,
    Repo,
    RepoFile,
    Snapshot,
)
from sensei_core.models.chunks import EMBEDDING_DIMENSIONS
from sensei_core.storage import Bucket, ObjectStorage
from sensei_engine.agent.loop import Answer, AskAgent, Budget, Done, Exchange, Mode
from sensei_engine.agent.tools import ToolContext
from sensei_engine.embed.provider import InputType
from sensei_engine.embed.provider import usage_ledger as embedding_ledger
from sensei_engine.llm.anthropic_client import (
    AnthropicClient,
    LLMError,
    LLMRequest,
    LLMUsage,
    Tier,
)
from sensei_engine.llm.ledger import usage_ledger

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/orgs/{org}", tags=["ask"])

STREAM_PREFIX = "ask:"
STREAM_SECONDS = 3_600  # how long a run's stream may be picked up again
STREAM_MAXLEN = 20_000
MAX_EXCHANGES = 6  # the earlier questions a run reads, newest last
TITLE_CHARS = 80
REF_TYPE = "agent_run"  # an Ask run's usage, by its run
JSON = "application/json"
FINISHED = frozenset({"finish"})
# A stream with nothing new for this long has lost its run (a restart mid-answer): it ends.
FOLLOW_IDLE_SECONDS = 300
STOP_SECONDS = 10  # how long a shutdown waits for runs to store that they were stopped

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


FeedbackKind = Literal[
    "helpful", "wrong_file", "outdated", "missing_context", "invented_symbol", "other"
]


class FeedbackIn(BaseModel):
    kind: FeedbackKind
    note: str = Field(default="", max_length=2_000)


class FeedbackOut(BaseModel):
    kind: FeedbackKind
    note: str
    created_at: datetime


class MessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    parts: list[dict[str, Any]]
    status: Literal["streaming", "done", "failed"]
    confidence: dict[str, Any] | None
    abstained: bool
    created_at: datetime
    citations: list[CitationOut]
    feedback: FeedbackOut | None = None  # what the signed-in user said of it


class ConversationOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    title: str
    shared: bool  # a share link opens it
    created_at: datetime
    updated_at: datetime


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ShareOut(BaseModel):
    token: str  # shown once: only its hash is kept
    path: str  # the web page it opens, "/s/<token>"


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


async def stop_runs(state: Any) -> None:
    """At shutdown: the runs under way stopped, each storing that it was (its message failed,
    its spend already in the ledger), for a few seconds at most."""
    held: set[asyncio.Task[None]] = getattr(state, "ask_runs", None) or set()
    if not held:
        return
    for task in held:
        task.cancel()
    await asyncio.wait(held, timeout=STOP_SECONDS)


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
    settings: Settings = request.app.state.settings
    bucket = TokenBucket(redis, name="ask", per_minute=settings.agent.ask_per_minute)
    wait = await bucket.take(str(access.user.id))
    if wait is not None:
        raise RateLimitedError(
            "Too many questions at once; try again in a moment.",
            headers={"Retry-After": str(wait)},
        )
    remaining = await remaining_today(
        db, redis, settings.agent, org_id=repo.org_id, user_id=access.user.id
    )
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
    # Only a question asked first is answered from (and kept in) the cache: a follow-up's words
    # mean what the conversation before them does, which is its asker's alone.
    embedding = None
    if not earlier:
        embedding = await _embedded(request, db, repo.org_id, body.message)
        kept = await answer_cache.lookup(
            db,
            snapshot_id=snapshot.id,
            mode=body.mode.value,
            question=body.message,
            embedding=embedding,
        )
        if kept is not None:
            return await _served(db, conversation, question, kept, snapshot)
    record = AgentRun(
        org_id=repo.org_id,
        repo_id=repo.id,
        user_id=user.id,
        kind="ask",
        question=body.message,
        mode=body.mode.value,
        created_at=datetime.now(UTC),
    )
    db.add(record)
    await db.flush()
    cap = Budget().max_cost_usd
    if remaining is not None:
        cap = remaining if cap is None else min(cap, remaining)
        await hold(redis, repo.org_id, record.id, cap)  # released as the run ends
    answer = ConversationMessage(
        org_id=repo.org_id,
        conversation_id=conversation.id,
        role="assistant",
        status="streaming",
        snapshot_id=snapshot.id,
        run_id=record.id,
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
        run_id=record.id,
        cap=cap,
        embedding=embedding,
    )
    key = f"{STREAM_PREFIX}{answer.id}"
    headers = {
        **HEADERS,
        HEADER[0]: HEADER[1],
        "x-conversation-id": str(conversation.id),
        "x-agent-run-id": str(record.id),
    }
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
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> ConversationList:
    """The signed-in user's conversations about the repository, the latest first; with ``q``,
    those whose title has it in it."""
    query = select(Conversation).where(
        Conversation.repo_id == repo.id, Conversation.user_id == access.user.id
    )
    if q and q.strip():
        query = query.where(Conversation.title.ilike(f"%{_escaped(q.strip())}%", escape="\\"))
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
    cited = await citations_of(db, messages)
    said: dict[uuid.UUID, FeedbackOut] = {}
    if messages:
        rows = await db.scalars(
            select(MessageFeedback).where(
                MessageFeedback.message_id.in_([m.id for m in messages]),
                MessageFeedback.user_id == access.user.id,
            )
        )
        said = {row.message_id: _feedback_out(row) for row in rows}
    return ConversationDetail(
        **_out(found).model_dump(),
        messages=[message_out(m, m.parts, cited.get(m.id, []), said.get(m.id)) for m in messages],
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    *, access: OrgAccessDep, db: DbSession, conversation_id: uuid.UUID, body: ConversationUpdate
) -> ConversationOut:
    """Rename a conversation."""
    found = await _owned(db, conversation_id, access.user.id)
    title = " ".join(body.title.split())
    if not title:
        raise InvalidError("A title needs a word in it.", code="title_empty")
    found.title = title
    await db.flush()
    await db.refresh(found)
    return _out(found)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    *, access: OrgAccessDep, db: DbSession, conversation_id: uuid.UUID
) -> Response:
    """Delete a conversation, its messages and their citations."""
    found = await _owned(db, conversation_id, access.user.id)
    await db.execute(delete(Conversation).where(Conversation.id == found.id))
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/share", response_model=ShareOut)
async def share_conversation(
    *, access: OrgAccessDep, db: DbSession, conversation_id: uuid.UUID
) -> ShareOut:
    """A link that opens the conversation read-only, signed in or not (its citations hidden
    from a viewer who can't read the repository). Asked again, a new link: the old one stops
    working (the "reset link")."""
    found = await _owned(db, conversation_id, access.user.id)
    token = new_share_token()
    found.share_token_hash = share_token_hash(token)
    await audit(
        db,
        AuditAction.CONVERSATION_SHARED,
        actor_user_id=access.user.id,
        org_id=access.org.id,
        target_type="conversation",
        target_id=found.id,
    )
    return ShareOut(token=token, path=f"/s/{token}")


@router.delete("/conversations/{conversation_id}/share", status_code=204)
async def unshare_conversation(
    *, access: OrgAccessDep, db: DbSession, conversation_id: uuid.UUID
) -> Response:
    """Stop the conversation's share link working."""
    found = await _owned(db, conversation_id, access.user.id)
    if found.share_token_hash is not None:
        found.share_token_hash = None
        await audit(
            db,
            AuditAction.CONVERSATION_UNSHARED,
            actor_user_id=access.user.id,
            org_id=access.org.id,
            target_type="conversation",
            target_id=found.id,
        )
    return Response(status_code=204)


# ---------------------------------------------------------------- feedback


@router.post("/messages/{message_id}/feedback", response_model=FeedbackOut)
async def give_feedback(
    *, access: OrgAccessDep, db: DbSession, message_id: uuid.UUID, body: FeedbackIn
) -> FeedbackOut:
    """Say what an answer was: helpful, or what was wrong with it. Said again, it replaces what
    was said before."""
    answer = await _answer_of(db, message_id, access.user.id)
    row = await db.scalar(
        select(MessageFeedback).where(
            MessageFeedback.message_id == answer.id, MessageFeedback.user_id == access.user.id
        )
    )
    if row is None:
        row = MessageFeedback(
            org_id=access.org.id, message_id=answer.id, user_id=access.user.id, kind=body.kind
        )
        db.add(row)
    row.kind = body.kind
    row.note = body.note.strip()
    await db.flush()
    await db.refresh(row)
    return _feedback_out(row)


@router.delete("/messages/{message_id}/feedback", status_code=204)
async def take_back_feedback(
    *, access: OrgAccessDep, db: DbSession, message_id: uuid.UUID
) -> Response:
    """Take back what was said of an answer."""
    answer = await _answer_of(db, message_id, access.user.id)
    await db.execute(
        delete(MessageFeedback).where(
            MessageFeedback.message_id == answer.id, MessageFeedback.user_id == access.user.id
        )
    )
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
    run_id: uuid.UUID
    cap: Decimal | None  # what the run may spend (held from the org's day while it runs)
    embedding: list[float] | None  # the question's, for the answer cache (a first question's)

    def start(self) -> None:
        task = asyncio.create_task(self._run())
        runs = _runs(self.request)
        runs.add(task)
        task.add_done_callback(runs.discard)

    async def _run(self) -> None:
        key = f"{STREAM_PREFIX}{self.message_id}"
        stream = UIStream(str(self.message_id), self.repo_name)
        failure: BaseException | None = None
        try:
            await self._publish(
                key, [*stream.start(), _conversation(self.conversation_id, self.run_id)]
            )
            async with self.sessions() as db:
                await set_user_context(db, self.user_id)
                await set_org_context(db, self.org_id)
                failure = await self._answer(db, key, stream)
        except BaseException as exc:  # stored, then a cancellation goes on
            failure = exc
        finally:
            await release(self.redis, self.org_id, self.run_id)
        if failure is None:
            return
        if not isinstance(failure, asyncio.CancelledError):
            log.error(
                "ask run failed",
                message_id=str(self.message_id),
                exc_info=(type(failure), failure, failure.__traceback__),
            )
        try:
            await self._publish(key, stream.failed(_said(failure)))
        except Exception:  # the stream is best effort; the row says it too
            log.warning("ask run's failure not streamed", message_id=str(self.message_id))
        await self._failed(stream, type(failure).__name__)
        if isinstance(failure, asyncio.CancelledError):
            raise failure

    async def _answer(self, db: AsyncSession, key: str, stream: UIStream) -> BaseException | None:
        """The run, in ``db``: its steps are kept whatever becomes of it (its failure is
        returned, not raised, so the session commits them), as is its spend (written as it
        happens, in sessions of its own)."""
        repo = await db.get(Repo, self.repo_id)
        snapshot = await db.get(Snapshot, self.snapshot_id)
        if repo is None or snapshot is None:
            return LookupError("the repository or its snapshot is gone")
        record = await db.get(AgentRun, self.run_id)
        if record is None:
            return LookupError("the run's record is gone")
        recorder = self._recorder(db, record)
        answer: Answer | None = None
        try:
            ctx = await self.contexts(self.request, db, repo, snapshot)
            agent = self._agent(ctx, recorder)
            async for event in agent.run(
                self.question, ctx, conversation=self.earlier, mode=self.mode
            ):
                await recorder.event(event)
                await self._publish(key, stream.encode(event))
                if isinstance(event, Done):
                    answer = event.answer
        except Exception as exc:  # the run's own failure: its steps stay
            await recorder.finish(None, error=type(exc).__name__)
            return exc
        await recorder.finish(answer)
        stored = await self._store(db, stream, answer, ctx.sha)
        if stored and answer is not None and not self.earlier and answer_cache.keepable(answer):
            async with db.begin_nested():  # a cache that can't keep it loses nothing else
                await answer_cache.keep(
                    db,
                    org_id=self.org_id,
                    repo_id=self.repo_id,
                    snapshot_id=self.snapshot_id,
                    mode=self.mode.value,
                    question=self.question,
                    embedding=self.embedding,
                    message_id=self.message_id,
                    cited=[c.cited.path for c in answer.citations],
                )
        return None

    async def _spend(self, used: LLMUsage) -> None:
        """The run's spend, in the usage ledger at once (its own short transaction): a run that
        fails, or is stopped, has still spent it, and the day's cap counts it."""
        async with self.sessions() as db:
            await set_user_context(db, self.user_id)
            await set_org_context(db, self.org_id)
            ledger = usage_ledger(
                db,
                search.cost_calculator(self.request),
                org_id=self.org_id,
                user_id=self.user_id,
                ref_type=REF_TYPE,
                ref_id=str(self.run_id),
            )
            await ledger(used)

    def _recorder(self, db: AsyncSession, record: AgentRun) -> Recorder:
        state = self.request.app.state
        storage: ObjectStorage | None = getattr(state, "storage", None)

        async def put(key: str, data: bytes) -> None:
            if storage is not None:
                await storage.put_bytes(Bucket.ARTIFACTS, key, data, content_type=JSON)

        settings: Settings = state.settings
        return Recorder(
            db,
            record,
            redis=self.redis,
            costs=search.cost_calculator(self.request),
            put=put if storage is not None else None,
            inline_bytes=settings.agent.inline_output_bytes,
        )

    def _agent(self, ctx: ToolContext, recorder: Recorder) -> AskAgent:
        costs = search.cost_calculator(self.request)

        async def usage(used: LLMUsage) -> None:
            await self._spend(used)
            await recorder.usage(used)

        return AskAgent(
            self.client,
            budget=dataclasses.replace(Budget(), max_cost_usd=self.cap),
            costs=costs,
            on_usage=usage,
            reranker=ctx.search.reranker,
        )

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
    ) -> bool:
        """The answer on its message (False: the conversation was deleted meanwhile)."""
        message = await db.get(ConversationMessage, self.message_id)
        if message is None:
            return False
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
        return True

    async def _failed(self, stream: UIStream, error: str) -> None:
        try:
            async with self.sessions() as db:
                await set_user_context(db, self.user_id)
                await set_org_context(db, self.org_id)
                await db.execute(
                    update(ConversationMessage)
                    .where(ConversationMessage.id == self.message_id)
                    .values(status="failed", parts=stream.parts)
                )
                await db.execute(
                    update(AgentRun)
                    .where(AgentRun.id == self.run_id)
                    .values(status="failed", error=error, finished_at=func.now())
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
            titled = await self.client.complete(request, on_usage=self._spend)
        except LLMError:
            return
        title = " ".join(titled.text.split()).strip(" \"'.")[:TITLE_CHARS]
        if title:
            await db.execute(
                update(Conversation)
                .where(Conversation.id == self.conversation_id)
                .values(title=title)
            )


# ---------------------------------------------------------------- the answer cache


async def _embedded(
    request: Request, db: AsyncSession, org_id: uuid.UUID, question: str
) -> list[float] | None:
    """The question's embedding, for near matches in the answer cache (None without
    embeddings, or when they can't be had: the cache then matches the same words only)."""
    provider = search.embedding_provider(request)
    if provider is None or provider.dims != EMBEDDING_DIMENSIONS:
        return None
    costs = search.cost_calculator(request)
    try:
        (vector,) = await provider.embed(
            [question],
            InputType.QUERY,
            on_usage=embedding_ledger(db, costs, org_id=org_id, ref_type=REF_TYPE),
        )
    except Exception:  # the cache is a shortcut, never what an answer stands on
        log.warning("question not embedded for the answer cache")
        return None
    return vector


async def _served(
    db: AsyncSession,
    conversation: Conversation,
    question: ConversationMessage,
    kept: AnswerCacheEntry,
    snapshot: Snapshot,
) -> EventSourceResponse:
    """The kept answer as this question's: its parts and citations copied, then streamed."""
    source = await db.get(ConversationMessage, kept.message_id)
    if source is None:
        raise RuntimeError("a kept answer's message is gone")
    cache = {"type": "data-cache", "id": "cache", "data": {"from": str(source.id)}}
    answer = ConversationMessage(
        org_id=conversation.org_id,
        conversation_id=conversation.id,
        role="assistant",
        status="done",
        parts=[*source.parts, cache],
        confidence=source.confidence,
        abstained=source.abstained,
        snapshot_id=snapshot.id,
    )
    db.add_all([question, answer])
    await db.flush()
    cited = await db.scalars(select(MessageCitation).where(MessageCitation.message_id == source.id))
    for row in list(cited):
        db.add(
            MessageCitation(
                org_id=row.org_id,
                message_id=answer.id,
                idx=row.idx,
                repo_id=row.repo_id,
                sha=row.sha,
                path=row.path,
                start_line=row.start_line,
                end_line=row.end_line,
                symbol_key=row.symbol_key,
                provenance="cache",
                verified=row.verified,
            )
        )
    await db.execute(
        update(Conversation).where(Conversation.id == conversation.id).values(updated_at=func.now())
    )
    await db.flush()
    headers = {
        **HEADERS,
        HEADER[0]: HEADER[1],
        "x-conversation-id": str(conversation.id),
        "x-answer-cache": "hit",
    }
    parts = replayed(str(answer.id), answer.parts)
    parts.insert(1, _conversation(conversation.id, None))
    return EventSourceResponse(_static(parts), ping=HEARTBEAT_SECONDS, headers=headers)


async def _static(parts: list[dict[str, Any]]) -> AsyncGenerator[ServerSentEvent]:
    for part in parts:
        yield ServerSentEvent(data=orjson.dumps(part).decode())
    yield ServerSentEvent(data=DONE)


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
    heard = time.monotonic()
    while True:
        batches = await cast(
            "Awaitable[list[Any] | None]",
            redis.xread({key: cursor}, count=200, block=READ_BLOCK_MS),  # pyright: ignore[reportUnknownMemberType]
        )
        if batches:
            heard = time.monotonic()
        elif time.monotonic() - heard > FOLLOW_IDLE_SECONDS:
            lost = {"type": "error", "errorText": "The answer stopped being written."}
            yield ServerSentEvent(data=orjson.dumps(lost).decode())
            yield ServerSentEvent(data=DONE)
            return
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


def _conversation(conversation_id: uuid.UUID, run_id: uuid.UUID | None) -> dict[str, Any]:
    """Which conversation the answer is in, and its run: what the client follows up in."""
    data = {"id": str(conversation_id), "runId": str(run_id) if run_id else None}
    return {"type": "data-conversation", "id": "conversation", "data": data}


async def _owned(db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID) -> Conversation:
    found = await db.get(Conversation, conversation_id)
    if found is None or found.user_id != user_id:
        raise NotFoundError("Conversation not found.", code="conversation_not_found")
    return found


async def _answer_of(
    db: AsyncSession, message_id: uuid.UUID, user_id: uuid.UUID
) -> ConversationMessage:
    """An answer in one of the user's conversations (a question, or another's, is not found)."""
    found = await db.scalar(
        select(ConversationMessage)
        .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
        .where(
            ConversationMessage.id == message_id,
            ConversationMessage.role == "assistant",
            Conversation.user_id == user_id,
        )
    )
    if found is None:
        raise NotFoundError("Answer not found.", code="message_not_found")
    return found


async def citations_of(
    db: AsyncSession, messages: list[ConversationMessage]
) -> dict[uuid.UUID, list[CitationOut]]:
    """The messages' citations, by message, in their order in each answer."""
    cited: dict[uuid.UUID, list[CitationOut]] = {}
    if not messages:
        return cited
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
    return cited


def message_out(
    m: ConversationMessage,
    parts: list[dict[str, Any]],
    citations: list[CitationOut],
    feedback: FeedbackOut | None = None,
) -> MessageOut:
    return MessageOut(
        id=m.id,
        role=cast("Literal['user', 'assistant']", m.role),
        parts=parts,
        status=cast("Literal['streaming', 'done', 'failed']", m.status),
        confidence=m.confidence,
        abstained=m.abstained,
        created_at=m.created_at,
        citations=citations,
        feedback=feedback,
    )


def _feedback_out(row: MessageFeedback) -> FeedbackOut:
    return FeedbackOut(
        kind=cast("FeedbackKind", row.kind), note=row.note, created_at=row.created_at
    )


def _escaped(words: str) -> str:
    """``words`` matched as they are by LIKE (its wildcards and escape character escaped)."""
    return words.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


def _said(exc: BaseException) -> str:
    if isinstance(exc, LLMError) and exc.transient:
        return "The model isn't answering right now; try again in a moment."
    return "The answer couldn't be written."


def _out(found: Conversation) -> ConversationOut:
    return ConversationOut(
        id=found.id,
        repo_id=found.repo_id,
        title=found.title,
        shared=found.share_token_hash is not None,
        created_at=found.created_at,
        updated_at=found.updated_at,
    )


def _cursor_time(cursor: str) -> datetime:
    try:
        moment = datetime.fromisoformat(cursor)
    except ValueError as exc:
        raise InvalidError("That cursor is no page.", code="invalid_cursor") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
