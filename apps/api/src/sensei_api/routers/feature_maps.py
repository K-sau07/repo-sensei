"""Feature maps (4.9): a repository's module maps at a commit (written after indexing), for the
overview and suggested questions."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from sensei_api.deps import DbSession
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import RefParam
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import FeatureMap

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["feature-maps"])


class FeatureMapOut(BaseModel):
    id: uuid.UUID
    key: str
    path: str
    title: str
    summary: str
    entry_points: list[dict[str, Any]]
    key_symbols: list[dict[str, Any]]
    data_flow: list[str]
    citations: list[dict[str, Any]]
    stale: bool


class FeatureMapList(BaseModel):
    sha: str
    maps: list[FeatureMapOut]


@router.get("/feature-maps", response_model=FeatureMapList)
async def feature_maps(*, repo: RepoDep, db: DbSession, ref: RefParam = None) -> FeatureMapList:
    """The modules' maps at the commit (the last index by default), by path."""
    snapshot = await resolve_snapshot(db, repo, ref)
    rows = await db.scalars(
        select(FeatureMap).where(FeatureMap.snapshot_id == snapshot.id).order_by(FeatureMap.path)
    )
    return FeatureMapList(
        sha=snapshot.commit_sha,
        maps=[
            FeatureMapOut(
                id=row.id,
                key=row.key,
                path=row.path,
                title=row.title,
                summary=row.summary,
                entry_points=row.entry_points,
                key_symbols=row.key_symbols,
                data_flow=row.data_flow,
                citations=row.citations,
                stale=row.stale,
            )
            for row in rows
        ],
    )
