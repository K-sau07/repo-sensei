"""The Living Constellation: every file of a snapshot as a point in 3D, clustered by directory.

Points come in pages: ``since`` is a cursor (how many points the caller has), and each answer
says where the next page starts (``next``) and how many points there are so far
(``available``). With ``job``, it is that index run's constellation, which grows while discovery
runs (read from Redis; the run's ``constellation`` events say when there is more) and is stored
with the snapshot once discovery is done. Otherwise ``ref`` picks an indexed snapshot (the last
index by default). A snapshot indexed before constellations were stored gets one laid out from
its file rows: the layout depends only on the paths, so it is the same picture.

``generation`` names the discovery the points belong to; a caller holding points of another
generation (discovery started over) drops them and reads from the start.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.deps import DbSession, RedisClient
from sensei_api.repos import RepoDep
from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.indexing.jobs import IndexJobService
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, RepoFile, Snapshot
from sensei_core.storage import Bucket, ObjectStorage, artifact_key
from sensei_engine.ingest.constellation import (
    ARTIFACT_NAME,
    LAYOUT,
    MAX_STORED_BYTES,
    Constellation,
    CorruptConstellationError,
    Directory,
    DirectoryLayout,
    Point,
    read_live,
)

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["constellation"])
log = get_logger(__name__)

PAGE_SIZE = 10_000
MAX_PAGE_SIZE = 50_000

Layer = Literal["ui", "api", "svc", "db", "other"]
Status = Literal["pending", "streaming", "stopped", "complete"]


class ConstellationDir(BaseModel):
    id: int = Field(description="What points' ``dir_cluster`` refers to.")
    path: str = Field(description='"" for the root.')
    parent: int | None
    depth: int
    x: float
    y: float
    z: float
    files: int = Field(description="Files directly inside.")


class ConstellationPoint(BaseModel):
    file_id: uuid.UUID
    path: str
    x: float
    y: float
    z: float
    layer: Layer
    role: str
    language: str | None
    dir_cluster: int
    size: int = Field(description="Bytes.")
    loc: int | None


class ConstellationOut(BaseModel):
    layout: Literal["directory"] = LAYOUT
    status: Status = Field(
        description="pending: nothing yet; streaming: discovery is finding files; stopped: the "
        "run ended before discovery finished; complete: every point."
    )
    commit_sha: str | None
    generation: str | None
    total: int | None = Field(description="Points there will be (known once the walk is done).")
    available: int = Field(description="Points there are now.")
    since: int
    next: int = Field(description="The cursor for the next page.")
    dirs: list[ConstellationDir] = Field(description="Every directory, on the first page only.")
    points: list[ConstellationPoint]


def dirs_out(dirs: list[Directory]) -> list[ConstellationDir]:
    return [
        ConstellationDir(
            id=index,
            path=d.path,
            parent=d.parent,
            depth=d.depth,
            x=d.position[0],
            y=d.position[1],
            z=d.position[2],
            files=d.files,
        )
        for index, d in enumerate(dirs)
    ]


def points_out(points: list[Point]) -> list[ConstellationPoint]:
    return [
        ConstellationPoint(
            file_id=p.file_id,
            path=p.path,
            x=p.position[0],
            y=p.position[1],
            z=p.position[2],
            layer=cast("Layer", p.layer),  # one of them by construction
            role=p.role,
            language=p.language,
            dir_cluster=p.directory,
            size=p.size,
            loc=p.loc,
        )
        for p in points
    ]


def _page(
    found: Constellation, *, commit_sha: str, status: Status, since: int, limit: int
) -> ConstellationOut:
    page = found.points[since : since + limit]
    return ConstellationOut(
        status=status,
        commit_sha=commit_sha,
        generation=found.generation,
        total=len(found.points),
        available=len(found.points),
        since=since,
        next=since + len(page),
        dirs=dirs_out(found.dirs) if since == 0 else [],
        points=points_out(page),
    )


def _storage(request: Request) -> ObjectStorage:
    storage: ObjectStorage = request.app.state.storage
    return storage


async def _from_rows(db: AsyncSession, snapshot: Snapshot) -> Constellation:
    """Laid out from the file rows (the same layout: it depends only on the paths)."""
    rows = (
        await db.execute(
            select(
                RepoFile.id,
                RepoFile.path,
                RepoFile.role,
                RepoFile.language,
                RepoFile.size,
                RepoFile.loc,
            ).where(RepoFile.snapshot_id == snapshot.id)
        )
    ).all()
    rows = sorted(rows, key=lambda row: row.path)  # discovery's order (code points, as Python)
    layout = DirectoryLayout(row.path for row in rows)
    points = [
        layout.point(
            file_id=row.id,
            path=row.path,
            role=row.role,
            language=row.language,
            size=row.size,
            loc=row.loc,
        )
        for row in rows
    ]
    return Constellation.build(f"files-{snapshot.id.hex}", layout, points)


async def _stored(request: Request, db: AsyncSession, snapshot: Snapshot) -> Constellation:
    key = artifact_key(snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, ARTIFACT_NAME)
    data = await _storage(request).get_bytes(Bucket.ARTIFACTS, key, max_bytes=MAX_STORED_BYTES)
    if data is not None:
        try:
            return Constellation.from_bytes(data)
        except CorruptConstellationError:
            log.warning("stored constellation unreadable", commit_sha=snapshot.commit_sha)
    return await _from_rows(db, snapshot)


@router.get("/constellation", response_model=ConstellationOut)
async def constellation(
    *,
    request: Request,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    since: Annotated[int, Query(ge=0, description="Points the caller has already.")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
    job: Annotated[
        uuid.UUID | None, Query(description="An index run: its constellation, while it forms.")
    ] = None,
    ref: Annotated[
        str | None,
        Query(max_length=255, description="Branch or commit (or its first 7+ characters)."),
    ] = None,
) -> ConstellationOut:
    """A page of the constellation's points (file, position, layer, directory cluster, size)."""
    if job is not None and ref is not None:
        raise InvalidError("Ask for a run or a ref, not both.", code="job_and_ref")
    if job is None:
        snapshot = await resolve_snapshot(db, repo, ref)
        found = await _stored(request, db, snapshot)
        return _page(
            found, commit_sha=snapshot.commit_sha, status="complete", since=since, limit=limit
        )

    run = await IndexJobService().get(db, repo, job)
    discovered = (run.stages or {}).get("discover", {}).get("status") == "done"
    snapshot = await db.get(Snapshot, run.snapshot_id) if run.snapshot_id else None
    commit_sha = snapshot.commit_sha if snapshot else None
    if discovered:
        if snapshot is None:
            raise NotFoundError("This run's snapshot is gone.", code="snapshot_not_found")
        found = await _stored(request, db, snapshot)
        return _page(
            found, commit_sha=snapshot.commit_sha, status="complete", since=since, limit=limit
        )

    active = run.status in ACTIVE_JOB_STATUSES
    live = await read_live(redis, run.id, since, limit)
    if live is None:
        return ConstellationOut(
            status="pending" if active else "stopped",
            commit_sha=commit_sha,
            generation=None,
            total=None,
            available=0,
            since=since,
            next=since,
            dirs=[],
            points=[],
        )
    return ConstellationOut(
        status="streaming" if active else "stopped",
        commit_sha=commit_sha,
        generation=live.generation,
        total=live.total,
        available=live.available,
        since=since,
        next=since + len(live.points),
        dirs=dirs_out(live.dirs),
        points=points_out(live.points),
    )
