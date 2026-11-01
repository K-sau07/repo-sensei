"""The Codebase Galaxy (6.9): ``GET …/galaxy?layout=semantic|structural`` (``ref`` as in
browsing), every file of the snapshot a star in 3D, in named clusters.

- **structural** is the constellation's layout (1.14: by folder), each star in its top folder's
  cluster.
- **semantic** places files by what their summaries mean (:mod:`sensei_engine.galaxy`), made in
  a worker and kept (the ``galaxy-semantic.msgpack`` artifact). Not kept yet but possible (the
  snapshot's summaries are embedded): it is asked for, and the answer is 409
  ``galaxy_computing`` with ``Retry-After``. Not possible (no summaries, or no embeddings):
  409 ``semantic_not_ready``.

A star carries its file's layer and language (for color) from the constellation, whichever the
layout.
"""

from __future__ import annotations

import gzip
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Annotated, Any, Literal, Protocol, cast

import msgpack
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import COMMIT_HEADER, RefParam
from sensei_api.routers.constellation import stored_constellation
from sensei_api.routers.graph import limit_graph
from sensei_core.errors import ConflictError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.jobs import galaxy_key
from sensei_core.logging import get_logger
from sensei_core.models import CodeChunk, Snapshot
from sensei_core.storage import Bucket, ObjectStorage, artifact_key
from sensei_engine.galaxy import Galaxy
from sensei_engine.ingest.constellation import Constellation

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["galaxy"])
log = get_logger(__name__)

ARTIFACT = "galaxy-semantic.msgpack"
MAX_ARTIFACT_BYTES = 128 << 20
RETRY_SECONDS = 15
KEY_TTL_SECONDS = 15 * 60
ROOT = "(root)"
_unpack = cast("Callable[[bytes], object]", msgpack.unpackb)  # pyright: ignore[reportUnknownMemberType]

Layout = Literal["semantic", "structural"]


class GalaxyJobs(Protocol):
    async def layout(
        self, org_id: uuid.UUID, repo_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> None: ...


class GalaxyStar(BaseModel):
    path: str
    x: float
    y: float
    z: float
    cluster: int
    layer: str
    language: str | None


class GalaxyCluster(BaseModel):
    id: int
    label: str
    x: float
    y: float
    z: float
    files: int


class GalaxyOut(BaseModel):
    layout: Layout
    sha: str
    stars: list[GalaxyStar]
    clusters: list[GalaxyCluster]


def _structural(found: Constellation) -> tuple[list[GalaxyStar], list[GalaxyCluster]]:
    """The constellation, each file in its top folder's cluster (the biggest first)."""
    members: defaultdict[str, list[int]] = defaultdict(list)
    for i, point in enumerate(found.points):
        members[point.path.split("/", 1)[0] if "/" in point.path else ROOT].append(i)
    order = sorted(members, key=lambda top: (-len(members[top]), top))
    ids = {top: i for i, top in enumerate(order)}
    stars: list[GalaxyStar] = []
    for point in found.points:
        top = point.path.split("/", 1)[0] if "/" in point.path else ROOT
        x, y, z = point.position
        stars.append(
            GalaxyStar(
                path=point.path, x=x, y=y, z=z, cluster=ids[top],
                layer=point.layer, language=point.language,
            )
        )  # fmt: skip
    clusters = [
        GalaxyCluster(
            id=ids[top],
            label=top,
            x=sum(found.points[i].position[0] for i in members[top]) / len(members[top]),
            y=sum(found.points[i].position[1] for i in members[top]) / len(members[top]),
            z=sum(found.points[i].position[2] for i in members[top]) / len(members[top]),
            files=len(members[top]),
        )
        for top in order
    ]
    return stars, clusters


def _semantic(galaxy: Galaxy, found: Constellation) -> tuple[list[GalaxyStar], list[GalaxyCluster]]:
    files = {p.path: p for p in found.points}
    stars = [
        GalaxyStar(
            path=s.path, x=s.x, y=s.y, z=s.z, cluster=s.cluster,
            layer=files[s.path].layer if s.path in files else "other",
            language=files[s.path].language if s.path in files else None,
        )
        for s in galaxy.stars
    ]  # fmt: skip
    clusters = [
        GalaxyCluster(id=c.id, label=c.label, x=c.x, y=c.y, z=c.z, files=c.files)
        for c in galaxy.clusters
    ]
    return stars, clusters


async def _kept(storage: ObjectStorage, snapshot: Snapshot) -> Galaxy | None:
    name = artifact_key(snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, ARTIFACT)
    data = await storage.get_bytes(Bucket.ARTIFACTS, name, max_bytes=MAX_ARTIFACT_BYTES)
    if not data:
        return None
    try:
        return Galaxy.from_json(cast("dict[str, Any]", _unpack(gzip.decompress(data))))
    except Exception:  # unreadable: made again
        log.warning("galaxy.unreadable", snapshot_id=str(snapshot.id), exc_info=True)
        return None


async def _ask_for(request: Request, redis: RedisClient, snapshot: Snapshot) -> None:
    """Have the semantic galaxy made, unless it is on its way already."""
    jobs: GalaxyJobs | None = getattr(request.app.state, "galaxy_jobs", None)
    if jobs is None:
        return
    key = galaxy_key(snapshot.id)
    if await redis.set(key, "1", nx=True, ex=KEY_TTL_SECONDS):
        await jobs.layout(snapshot.org_id, snapshot.repo_id, snapshot.id)


@router.get("/galaxy", response_model=GalaxyOut)
async def galaxy(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    layout: Annotated[Layout, Query()] = "semantic",
    ref: RefParam = None,
) -> GalaxyOut:
    """Every file a star, in clusters (see the module)."""
    await limit_graph(request, redis, access)
    snapshot = await resolve_snapshot(db, repo, ref)
    found = await stored_constellation(request, db, snapshot)
    if layout == "structural":
        stars, clusters = _structural(found)
    else:
        storage: ObjectStorage = request.app.state.storage
        kept = await _kept(storage, snapshot)
        if kept is None:
            embedded = await db.scalar(
                select(CodeChunk.id)
                .where(
                    CodeChunk.repo_id == snapshot.repo_id,
                    CodeChunk.snapshot_id == snapshot.id,
                    CodeChunk.view == "summary",
                    CodeChunk.embedding.is_not(None),
                )
                .limit(1)
            )
            if embedded is None:
                raise ConflictError(
                    "The semantic layout needs this commit's summaries, embedded.",
                    code="semantic_not_ready",
                )
            await _ask_for(request, redis, snapshot)
            raise ConflictError(
                "The semantic layout is being worked out; try again in a moment.",
                code="galaxy_computing",
                headers={"Retry-After": str(RETRY_SECONDS)},
            )
        stars, clusters = _semantic(kept, found)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    return GalaxyOut(layout=layout, sha=snapshot.commit_sha, stars=stars, clusters=clusters)
