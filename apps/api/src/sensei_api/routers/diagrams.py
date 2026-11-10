"""Diagrams (6.1): a flow drawn from the graph, checked against it, kept.

``POST …/diagrams`` draws one, from a trace (``from_trace``: its entry and options, and which
of its paths) or a question (its trace when one follows from the words; else a bounded walk out
from the symbols it names). The fast model labels the subgraph; the validator keeps only what
the graph proves, or what it says why it infers (see :mod:`sensei_engine.diagrams`). It is
kept at the snapshot it was drawn at, with how much of it the graph proves (``verified_ratio``)
and Mermaid compiled from it, each node linked to its code at that commit.

A repository's diagrams are its org's: any member who can read the repository lists and opens
them; who drew one, or an admin, renames, deletes and shares it. A share link opens it
read-only (``GET /d/{token}``, in :mod:`sensei_api.routers.shared`). Drawing one counts against
the user's graph requests per minute, and its model use is in the usage ledger.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from sensei_api import streams
from sensei_api.deps import DbSession, RedisClient, Writable
from sensei_api.orgs import OrgAccessDep
from sensei_api.ratelimit import TokenBucket
from sensei_api.repos import RepoDep
from sensei_api.routers import search
from sensei_api.routers.browse import RefParam
from sensei_api.routers.deps import snapshot_roots
from sensei_api.routers.graph import limit_graph
from sensei_api.sharing import new_share_token, share_token_hash
from sensei_api.spend import remaining_today
from sensei_core.audit import AuditAction, audit
from sensei_core.config import Settings
from sensei_core.errors import ForbiddenError, InvalidError, NotFoundError, RateLimitedError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import Diagram, DiagramKind, DiagramSource, Repo, Role
from sensei_core.orgs import model_choices
from sensei_core.orgs.service import OrgAccess
from sensei_core.storage import ObjectStorage
from sensei_engine.architecture import whole_app
from sensei_engine.diagrams import (
    DiagramError,
    DiagramSpec,
    SpecNode,
    Subgraph,
    from_entries,
    from_trace,
    generate,
    mermaid,
)
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.graph.store import GraphStore
from sensei_engine.graph.trace import (
    Direction,
    EntryKind,
    TraceError,
    feature_entries,
    trace_feature,
)
from sensei_engine.llm.anthropic_client import AnthropicClient, LLMUsage
from sensei_engine.llm.ledger import usage_ledger

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["diagrams"])

REF_TYPE = "diagram"
LIST_LIMIT = 50


class FromTrace(BaseModel):
    source: str = Field(alias="from", min_length=1, max_length=2000)
    kind: EntryKind = "auto"
    to: str | None = Field(default=None, min_length=1, max_length=2000)
    feature: str | None = Field(default=None, min_length=1, max_length=2000)
    direction: Direction = "both"
    path: int = Field(default=1, ge=1, le=3, description="Which of its paths (1: the surest).")


class DiagramIn(BaseModel):
    from_trace: FromTrace | None = None
    question: str | None = Field(default=None, min_length=1, max_length=2000)
    whole_app: bool | None = Field(
        default=None,
        description="The whole app's architecture: its modules by tier and how they depend on "
        "each other, from the graph alone (no model: free). Drawn as an architecture.",
    )
    kind: DiagramKind = DiagramKind.FLOWCHART
    title: str | None = Field(default=None, min_length=1, max_length=200)


class DiagramUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class DiagramSummary(BaseModel):
    id: uuid.UUID
    title: str
    kind: DiagramKind
    source: DiagramSource
    commit_sha: str
    verified_ratio: float
    nodes: int
    edges: int
    created_by: uuid.UUID | None
    shared: bool
    created_at: datetime


class DiagramOut(DiagramSummary):
    prompt: dict[str, Any]
    spec: dict[str, Any]
    mermaid: str


class DiagramList(BaseModel):
    diagrams: list[DiagramSummary]


class DiagramShareOut(BaseModel):
    token: str  # shown once: only its hash is kept
    path: str  # the web page it opens, "/d/<token>"


def _client(request: Request) -> AnthropicClient:
    """The API's model client (shared with Ask's)."""
    state = request.app.state
    found: AnthropicClient | None = getattr(state, "ask_client", None)
    if found is None:
        found = AnthropicClient(state.settings.anthropic)
        state.ask_client = found
    return found


def code_link(org: str, repo: str, sha: str) -> Any:
    """Where a node's code opens on the web, at the diagram's commit."""

    def link(node: SpecNode) -> str | None:
        if not node.path:
            return None
        lines = f"{node.start_line}-{node.end_line}" if node.end_line > node.start_line else ""
        query = f"?ref={sha}" + (f"&L={lines}" if lines else f"&L={node.start_line}")
        return f"/{quote(org)}/{quote(repo)}/code/{quote(node.path)}{query}"

    return link


def summary_of(found: Diagram) -> DiagramSummary:
    spec = found.spec
    return DiagramSummary(
        id=found.id,
        title=found.title,
        kind=found.kind,
        source=found.source,
        commit_sha=found.commit_sha,
        verified_ratio=found.verified_ratio,
        nodes=len(spec.get("nodes", [])),
        edges=len(spec.get("edges", [])),
        created_by=found.created_by,
        shared=found.share_token_hash is not None,
        created_at=found.created_at,
    )


def diagram_out(found: Diagram, org: str, repo: Repo, *, links: bool = True) -> DiagramOut:
    spec = DiagramSpec.from_json(found.spec)
    drawn = mermaid(spec, link=code_link(org, repo.slug, found.commit_sha) if links else None)
    return DiagramOut(
        **summary_of(found).model_dump(), prompt=found.prompt, spec=found.spec, mermaid=drawn
    )


def _subgraph(graph: GraphSnapshot, body: DiagramIn) -> tuple[Subgraph, DiagramSource]:
    """What to draw (see the module); raises :class:`NotFoundError` when nothing is found."""
    if body.from_trace is not None:
        asked = body.from_trace
        try:
            result = trace_feature(
                graph, asked.source, kind=asked.kind, to=asked.to,
                direction=asked.direction, feature=asked.feature,
            )  # fmt: skip
        except TraceError as exc:
            raise NotFoundError(str(exc), code="trace_entry_not_found") from None
        if not result.paths:
            raise NotFoundError("The trace found no path to draw.", code="diagram_nothing_found")
        path = result.paths[min(asked.path, len(result.paths)) - 1]
        return from_trace(graph, path, f"the flow from {asked.source}"), DiagramSource.TRACE
    question = body.question or ""
    try:
        result = trace_feature(graph, question, kind="auto", feature=question)
    except TraceError:
        result = None
    if result is not None and result.paths:
        return from_trace(graph, result.paths[0], question), DiagramSource.QUESTION
    entries = feature_entries(graph, question)
    if not entries:
        raise NotFoundError(
            "Nothing in the code graph matches the question.", code="diagram_nothing_found"
        )
    return from_entries(graph, entries, question), DiagramSource.QUESTION


@router.post("/diagrams", response_model=DiagramOut, status_code=201, dependencies=[Writable])
async def create_diagram(
    *,
    request: Request,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    body: DiagramIn,
    ref: RefParam = None,
) -> DiagramOut:
    """Draw a diagram from a trace, a question or the whole app, at the index (or ``ref``), and
    keep it."""
    given = sum([body.from_trace is not None, body.question is not None, bool(body.whole_app)])
    if given != 1:
        raise InvalidError(
            "Give one of from_trace, a question or whole_app.", code="diagram_source"
        )
    await limit_graph(request, redis, access)
    if body.whole_app:
        return await _whole_app(request, access=access, repo=repo, db=db, body=body, ref=ref)
    # A model labels it (up to three calls): its own bucket, within the org's day of spend.
    settings: Settings = request.app.state.settings
    bucket = TokenBucket(redis, name="diagram", per_minute=settings.agent.diagrams_per_minute)
    wait = await bucket.take(str(access.user.id))
    if wait is not None:
        raise RateLimitedError(
            "Too many diagrams at once; try again in a moment.",
            headers={"Retry-After": str(wait)},
        )
    await remaining_today(
        db, redis, settings.agent, org_id=repo.org_id, user_id=access.user.id,
        warn_in=lambda: streams.short_session(request),
    )  # fmt: skip
    snapshot = await resolve_snapshot(db, repo, ref)
    store: GraphStore = request.app.state.graph_store
    storage: ObjectStorage = request.app.state.storage
    graph = await store.get(db, storage, snapshot)
    sub, source = await asyncio.to_thread(_subgraph, graph, body)
    used: list[LLMUsage] = []

    async def note(usage: LLMUsage) -> None:
        used.append(usage)

    try:
        floor = (await model_choices.load(db, repo.org_id)).diagrams
        made = await generate(
            _client(request), sub, graph, body.kind.value, on_usage=note, floor=floor
        )
    except DiagramError as exc:
        raise NotFoundError(str(exc), code="diagram_nothing_found") from None
    spec = made.spec
    if body.title:
        spec.title = body.title
    found = Diagram(
        org_id=repo.org_id,
        repo_id=repo.id,
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        title=spec.title,
        kind=body.kind,
        source=source,
        prompt=(
            {"from_trace": body.from_trace.model_dump(by_alias=True, exclude_none=True)}
            if body.from_trace is not None
            else {"question": body.question}
        ),
        spec=spec.as_json(),
        verified_ratio=spec.verified_ratio,
        created_by=access.user.id,
    )
    db.add(found)
    await db.flush()
    await db.refresh(found)
    ledger = usage_ledger(
        db, search.cost_calculator(request), org_id=repo.org_id, user_id=access.user.id,
        ref_type=REF_TYPE, ref_id=str(found.id),
    )  # fmt: skip
    for usage in used:
        await ledger(usage)
    return diagram_out(found, access.org.slug, repo)


async def _whole_app(
    request: Request,
    *,
    access: OrgAccess,
    repo: Repo,
    db: DbSession,
    body: DiagramIn,
    ref: str | None,
) -> DiagramOut:
    """The whole app's architecture, from the graph alone (no model, no spend)."""
    snapshot = await resolve_snapshot(db, repo, ref)
    store: GraphStore = request.app.state.graph_store
    storage: ObjectStorage = request.app.state.storage
    graph = await store.get(db, storage, snapshot)
    roots = await snapshot_roots(request, db, snapshot)
    title = body.title or f"Architecture of {repo.full_name}"
    spec = await asyncio.to_thread(whole_app, graph, roots, title)
    if not spec.nodes:
        raise NotFoundError(
            "The graph has no modules to draw yet; index the repository first.",
            code="diagram_nothing_found",
        )
    found = Diagram(
        org_id=repo.org_id, repo_id=repo.id, snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha, title=spec.title, kind=DiagramKind.ARCHITECTURE,
        source=DiagramSource.REPO, prompt={"whole_app": True}, spec=spec.as_json(),
        verified_ratio=spec.verified_ratio, created_by=access.user.id,
    )  # fmt: skip
    db.add(found)
    await db.flush()
    await db.refresh(found)
    return diagram_out(found, access.org.slug, repo)


@router.get("/diagrams", response_model=DiagramList)
async def list_diagrams(
    *, repo: RepoDep, db: DbSession, limit: Annotated[int, Query(ge=1, le=LIST_LIMIT)] = 20
) -> DiagramList:
    """The repository's diagrams, newest first."""
    rows = await db.scalars(
        select(Diagram)
        .where(Diagram.repo_id == repo.id)
        .order_by(Diagram.created_at.desc(), Diagram.id.desc())
        .limit(limit)
    )
    return DiagramList(diagrams=[summary_of(d) for d in rows])


async def _found(db: DbSession, repo: Repo, diagram_id: uuid.UUID) -> Diagram:
    found = await db.scalar(
        select(Diagram).where(Diagram.id == diagram_id, Diagram.repo_id == repo.id)
    )
    if found is None:
        raise NotFoundError("No such diagram.", code="diagram_not_found")
    return found


def _may_change(access: OrgAccess, found: Diagram) -> None:
    """Who drew it, or an admin."""
    if found.created_by != access.user.id and not access.role.at_least(Role.ADMIN):
        raise ForbiddenError(
            "Only who drew this diagram, or an admin, can change it.", code="diagram_not_yours"
        )


@router.get("/diagrams/{diagram_id}", response_model=DiagramOut)
async def get_diagram(
    *, access: OrgAccessDep, repo: RepoDep, db: DbSession, diagram_id: uuid.UUID
) -> DiagramOut:
    """A diagram: its spec and its Mermaid."""
    return diagram_out(await _found(db, repo, diagram_id), access.org.slug, repo)


@router.patch("/diagrams/{diagram_id}", response_model=DiagramOut)
async def rename_diagram(
    *,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    diagram_id: uuid.UUID,
    body: DiagramUpdate,
) -> DiagramOut:
    found = await _found(db, repo, diagram_id)
    _may_change(access, found)
    title = " ".join(body.title.split())
    found.title = title
    found.spec = {**found.spec, "title": title}
    await db.flush()
    await db.refresh(found)
    return diagram_out(found, access.org.slug, repo)


@router.delete("/diagrams/{diagram_id}", status_code=204)
async def delete_diagram(
    *, access: OrgAccessDep, repo: RepoDep, db: DbSession, diagram_id: uuid.UUID
) -> Response:
    found = await _found(db, repo, diagram_id)
    _may_change(access, found)
    await db.delete(found)
    return Response(status_code=204)


@router.post("/diagrams/{diagram_id}/share", response_model=DiagramShareOut)
async def share_diagram(
    *, access: OrgAccessDep, repo: RepoDep, db: DbSession, diagram_id: uuid.UUID
) -> DiagramShareOut:
    """A link that opens the diagram read-only, signed in or not (its code hidden from a viewer
    who can't read the repository). Asked again, a new link: the old one stops working."""
    found = await _found(db, repo, diagram_id)
    _may_change(access, found)
    token = new_share_token()
    found.share_token_hash = share_token_hash(token)
    await audit(
        db, AuditAction.DIAGRAM_SHARED, actor_user_id=access.user.id, org_id=access.org.id,
        target_type="diagram", target_id=found.id,
    )  # fmt: skip
    return DiagramShareOut(token=token, path=f"/d/{token}")


@router.delete("/diagrams/{diagram_id}/share", status_code=204)
async def unshare_diagram(
    *, access: OrgAccessDep, repo: RepoDep, db: DbSession, diagram_id: uuid.UUID
) -> Response:
    found = await _found(db, repo, diagram_id)
    _may_change(access, found)
    if found.share_token_hash is not None:
        found.share_token_hash = None
        await audit(
            db, AuditAction.DIAGRAM_UNSHARED, actor_user_id=access.user.id, org_id=access.org.id,
            target_type="diagram", target_id=found.id,
        )  # fmt: skip
    return Response(status_code=204)
