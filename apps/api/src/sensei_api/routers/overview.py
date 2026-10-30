"""A repository's overview, once indexed (4.13): what it does (its directory summaries), how
it is laid out (its modules, each one's layers, and how much of the graph runs between them),
its feature maps, questions worth asking, its health and its last index.

Everything comes from what the index left: the summaries stage's rollups (an artifact; none
when that stage was skipped), the graph, the feature maps and the index job's stage details.
Nothing is made up when a piece is missing: it is empty or null. Freshness (commits since the
index) isn't tracked yet: pushes are stored, not acted on, so ``behind`` is always null.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime
from typing import Annotated, Any, cast

import orjson
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sensei_api.deps import DbSession, RedisClient
from sensei_api.repos import RepoDep
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import (
    CodeEdge,
    CodeNode,
    FeatureMap,
    IndexJob,
    JobStatus,
    Repo,
    RepoFile,
    Snapshot,
)
from sensei_core.models.graph import EdgeType, NodeKind, Provenance
from sensei_core.models.indexing import Stage
from sensei_core.storage import Bucket, ObjectStorage, artifact_key
from sensei_engine.ingest.constellation import layer_for
from sensei_engine.llm import rollups

router = APIRouter(prefix="/orgs/{org}", tags=["overview"])

MAX_MODULES = 12
MAX_DEPTH = 3
DOMINANT = 0.6  # a level where one module holds this share of the files or more is too coarse
MAX_ROLLUPS_BYTES = 32 * 1024 * 1024
MAX_QUESTIONS = 5
PRECISE = frozenset(
    {
        Provenance.SCIP,
        Provenance.LSP,
        Provenance.RUNTIME,
        Provenance.CONTRACT,
        Provenance.IMPORT_RESOLVED,
    }
)
INFERRED = frozenset({Provenance.LLM_INFERRED})
# The edges that say how code depends on code (not where it sits).
DEPENDS = (
    EdgeType.IMPORTS,
    EdgeType.CALLS,
    EdgeType.REFERENCES,
    EdgeType.INHERITS,
    EdgeType.IMPLEMENTS,
    EdgeType.RENDERS,
    EdgeType.USES_HOOK,
    EdgeType.INJECTS,
    EdgeType.ISSUES_REQUEST,
    EdgeType.TARGETS,
    EdgeType.HANDLED_BY,
    EdgeType.READS,
    EdgeType.WRITES,
)
LAYERS = ("ui", "api", "svc", "db", "other")
CACHE_PREFIX = "overview:"
CACHE_SECONDS = 24 * 3600


# ---------------------------------------------------------------- shapes


class DirectorySummaryOut(BaseModel):
    path: str
    summary: str


class SummaryOut(BaseModel):
    text: str
    # What it was written from: the top-level directories' own summaries.
    directories: list[DirectorySummaryOut]


class ModuleOut(BaseModel):
    path: str  # a directory ("." for files at the top)
    files: int
    layers: dict[str, int]  # files per layer: ui, api, svc, db, other


class ModuleLinkOut(BaseModel):
    source: str
    target: str
    edges: int


class ArchitectureOut(BaseModel):
    modules: list[ModuleOut]
    links: list[ModuleLinkOut]
    rest: int  # files in modules past the first few


class FeatureMapCardOut(BaseModel):
    key: str
    path: str
    title: str
    summary: str
    stale: bool


class LanguageHealthOut(BaseModel):
    language: str
    files: int
    with_symbols: int  # files the symbols stage found definitions in


class EdgeHealthOut(BaseModel):
    total: int
    precise: int  # a compiler's, the runtime's, a contract's, or an import resolved
    heuristic: int  # matched by name or by a receiver's type, guessed from a URL
    inferred: int  # a model's


class LinkHealthOut(BaseModel):
    client_calls: int | None  # HTTP calls the client makes
    linked: int | None  # of them, tied to the endpoint they call
    unresolved_edges: int | None  # graph and links edges left without a target


class FreshnessOut(BaseModel):
    indexed_at: datetime | None
    behind: int | None  # commits on the branch since the index (not tracked yet: null)


class HealthOut(BaseModel):
    languages: list[LanguageHealthOut]
    edges: EdgeHealthOut
    links: LinkHealthOut
    freshness: FreshnessOut


class StageOut(BaseModel):
    name: str
    status: str


class IndexSummaryOut(BaseModel):
    job_id: uuid.UUID
    kind: str
    started_at: datetime | None
    finished_at: datetime | None
    stages: list[StageOut]


class OverviewOut(BaseModel):
    sha: str
    summary: SummaryOut | None  # None: the summaries stage didn't run (or wrote nothing)
    architecture: ArchitectureOut
    feature_maps: list[FeatureMapCardOut]
    questions: list[str]
    health: HealthOut
    index: IndexSummaryOut | None


# ---------------------------------------------------------------- the endpoint


@router.get("/repos/{repo}/overview", response_model=OverviewOut)
async def overview(
    *,
    request: Request,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    ref: Annotated[str | None, Query(max_length=255)] = None,
) -> OverviewOut:
    """The repository at its last index (or at ``ref``), for its overview page. An indexed
    snapshot doesn't change, so its overview is kept (per snapshot and the index run that
    finished it) for a day: only the first look reads the whole graph."""
    snapshot = await resolve_snapshot(db, repo, ref)
    job = await _job(db, snapshot)
    key = f"{CACHE_PREFIX}{snapshot.id}:{job.id if job is not None else '-'}"
    try:
        kept = await redis.get(key)
    except RedisError:
        kept = None
    if kept is not None:
        return OverviewOut.model_validate_json(kept)
    found = await _overview(request, repo, db, snapshot, job)
    with suppress(RedisError):  # unkept, it is served all the same and read again next time
        await redis.set(key, found.model_dump_json(), ex=CACHE_SECONDS)
    return found


async def _overview(
    request: Request, repo: Repo, db: AsyncSession, snapshot: Snapshot, job: IndexJob | None
) -> OverviewOut:
    files = list(
        await db.execute(
            select(RepoFile.id, RepoFile.path, RepoFile.role, RepoFile.language).where(
                RepoFile.snapshot_id == snapshot.id
            )
        )
    )
    paths = [row.path for row in files]
    maps = list(
        await db.scalars(
            select(FeatureMap)
            .where(FeatureMap.snapshot_id == snapshot.id)
            .order_by(FeatureMap.path)
        )
    )
    return OverviewOut(
        sha=snapshot.commit_sha,
        summary=await _summary(request, repo, snapshot),
        architecture=await _architecture(db, snapshot, [(r.path, r.role) for r in files]),
        feature_maps=[
            FeatureMapCardOut(
                key=m.key, path=m.path, title=m.title, summary=m.summary, stale=m.stale
            )
            for m in maps
        ],
        questions=await _questions(db, snapshot, maps, paths),
        health=await _health(db, snapshot, job, [(r.id, r.language) for r in files]),
        index=_index(job),
    )


# ---------------------------------------------------------------- what it does


async def _summary(request: Request, repo: Repo, snapshot: Snapshot) -> SummaryOut | None:
    storage: ObjectStorage | None = getattr(request.app.state, "storage", None)
    if storage is None:
        return None
    key = artifact_key(repo.org_id, repo.id, snapshot.commit_sha, rollups.ARTIFACT)
    data = await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=MAX_ROLLUPS_BYTES)
    if data is None:
        return None
    directories = {
        str(d["path"]): str(d["summary"])
        for d in orjson.loads(data).get("directories", [])
        if d.get("summary")
    }
    root = directories.get(rollups.ROOT)
    if not root:
        return None
    top = [
        DirectorySummaryOut(path=path, summary=text)
        for path, text in sorted(directories.items())
        if path != rollups.ROOT and "/" not in path
    ]
    return SummaryOut(text=root, directories=top)


# ---------------------------------------------------------------- how it is laid out


def modules_of(paths: Sequence[str]) -> tuple[int, dict[str, str]]:
    """The depth the repository reads best at (the shallowest whose largest directory holds
    less than :data:`DOMINANT` of the files and that has at least three, up to
    :data:`MAX_DEPTH`) and each file's module at it."""
    chosen = MAX_DEPTH
    for depth in range(1, MAX_DEPTH + 1):
        sizes = Counter(_module(path, depth) for path in paths)
        if len(sizes) >= 3 and max(sizes.values()) < DOMINANT * len(paths):
            chosen = depth
            break
    return chosen, {path: _module(path, chosen) for path in paths}


def _module(path: str, depth: int) -> str:
    parts = path.split("/")[:-1]  # its directories
    return "/".join(parts[:depth]) if parts else rollups.ROOT


async def _architecture(
    db: AsyncSession, snapshot: Snapshot, files: list[tuple[str, str]]
) -> ArchitectureOut:
    _, module_of = modules_of([path for path, _ in files])
    sizes = Counter(module_of.values())
    shown = [path for path, _ in sizes.most_common(MAX_MODULES)]
    layers: dict[str, Counter[str]] = {path: Counter() for path in shown}
    for path, role in files:
        held = layers.get(module_of[path])
        if held is not None:
            held[layer_for(role)] += 1
    src, dst = aliased(CodeNode), aliased(CodeNode)
    src_file, dst_file = aliased(RepoFile), aliased(RepoFile)
    pairs = await db.execute(
        select(src_file.path, dst_file.path, func.count())
        .select_from(CodeEdge)
        .join(src, src.id == CodeEdge.src_id)
        .join(dst, dst.id == CodeEdge.dst_id)
        .join(src_file, src_file.id == src.file_id)
        .join(dst_file, dst_file.id == dst.file_id)
        .where(
            CodeEdge.snapshot_id == snapshot.id,
            CodeEdge.type.in_(DEPENDS),
            src.file_id != dst.file_id,
        )
        .group_by(src_file.path, dst_file.path)
    )
    between: Counter[tuple[str, str]] = Counter()
    for source_path, target_path, count in pairs:
        source, target = module_of.get(source_path), module_of.get(target_path)
        if source in layers and target in layers and source != target:
            between[(source, target)] += int(count)
    return ArchitectureOut(
        modules=[
            ModuleOut(
                path=path,
                files=sizes[path],
                layers={layer: layers[path][layer] for layer in LAYERS if layers[path][layer]},
            )
            for path in shown
        ],
        links=[
            ModuleLinkOut(source=source, target=target, edges=count)
            for (source, target), count in between.most_common()
        ],
        rest=sum(sizes.values()) - sum(sizes[path] for path in shown),
    )


# ---------------------------------------------------------------- questions


async def _questions(
    db: AsyncSession, snapshot: Snapshot, maps: list[FeatureMap], paths: list[str]
) -> list[str]:
    """Questions this repository answers well: how its feature maps' modules work, what its
    endpoints do; where it starts, if nothing else."""
    asked = [f"How does {m.title[:1].lower()}{m.title[1:]} work?" for m in maps[:3] if m.title]
    endpoints = await db.scalars(
        select(CodeNode.attrs)
        .where(CodeNode.snapshot_id == snapshot.id, CodeNode.kind == NodeKind.API_ENDPOINT)
        .order_by(CodeNode.qualified_name)
        .limit(2)
    )
    for attrs in endpoints:
        method, path = attrs.get("method"), attrs.get("path_norm")
        if method and path:
            asked.append(f"What happens when {str(method).upper()} {path} is called?")
    if not asked and paths:
        asked.append("Where does this repository start, and what does it do first?")
    return asked[:MAX_QUESTIONS]


# ---------------------------------------------------------------- health


async def _health(
    db: AsyncSession,
    snapshot: Snapshot,
    job: IndexJob | None,
    files: list[tuple[uuid.UUID, str | None]],
) -> HealthOut:
    defined = set(
        await db.scalars(
            select(CodeNode.file_id)
            .where(CodeNode.snapshot_id == snapshot.id, CodeNode.file_id.is_not(None))
            .distinct()
        )
    )
    per: dict[str, list[int]] = {}
    for file_id, language in files:
        if language:
            held = per.setdefault(language, [0, 0])
            held[0] += 1
            held[1] += file_id in defined
    rows = await db.execute(
        select(CodeEdge.provenance, func.count())
        .where(CodeEdge.snapshot_id == snapshot.id)
        .group_by(CodeEdge.provenance)
    )
    by_provenance: dict[Provenance, int] = {row[0]: int(row[1]) for row in rows}
    total = sum(by_provenance.values())
    precise = sum(n for p, n in by_provenance.items() if p in PRECISE)
    inferred = sum(n for p, n in by_provenance.items() if p in INFERRED)
    return HealthOut(
        languages=[
            LanguageHealthOut(language=language, files=counts[0], with_symbols=counts[1])
            for language, counts in sorted(per.items(), key=lambda kv: -kv[1][0])
        ],
        edges=EdgeHealthOut(
            total=total, precise=precise, heuristic=total - precise - inferred, inferred=inferred
        ),
        links=_links(job),
        freshness=FreshnessOut(indexed_at=snapshot.ready_at, behind=None),
    )


def _dict(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _count(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _details(job: IndexJob | None, stage: str) -> dict[str, Any]:
    stages = _dict(job.stages if job is not None else None)
    return _dict(_dict(stages.get(stage)).get("details"))


def _links(job: IndexJob | None) -> LinkHealthOut:
    graph, links = _details(job, "graph"), _details(job, "links")
    urls = _dict(_dict(links.get("linkers")).get("urls"))
    counted = [n for d in (graph, links) if (n := _count(d.get("unresolved_edges"))) is not None]
    return LinkHealthOut(
        client_calls=_count(urls.get("calls")),
        linked=_count(urls.get("linked")),
        unresolved_edges=sum(counted) if counted else None,
    )


# ---------------------------------------------------------------- the last index


async def _job(db: AsyncSession, snapshot: Snapshot) -> IndexJob | None:
    return await db.scalar(
        select(IndexJob)
        .where(IndexJob.snapshot_id == snapshot.id, IndexJob.status == JobStatus.SUCCEEDED)
        .order_by(IndexJob.created_at.desc())
        .limit(1)
    )


def _index(job: IndexJob | None) -> IndexSummaryOut | None:
    if job is None:
        return None
    recorded = _dict(job.stages)
    order = [stage.value for stage in Stage]  # as they run (the column keeps no order)
    stages = [
        StageOut(name=name, status=str(_dict(recorded[name]).get("status", "")))
        for name in sorted(recorded, key=lambda n: order.index(n) if n in order else len(order))
    ]
    return IndexSummaryOut(
        job_id=job.id,
        kind=str(job.kind),
        started_at=job.started_at,
        finished_at=job.finished_at,
        stages=stages,
    )
