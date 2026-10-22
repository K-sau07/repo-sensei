"""Staff-only routes. Everyone else gets a 404, as if they didn't exist."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from sensei_api.auth import CurrentStaff
from sensei_api.deps import DbSession
from sensei_api.errors import NotFound
from sensei_core.models import EvalRun

router = APIRouter(prefix="/internal", tags=["internal"])
MAX_PAGE_SIZE = 100


class EvalRunSummary(BaseModel):
    id: uuid.UUID
    suite: str
    suite_version: int
    retriever: str
    config: dict[str, Any]
    repo_sha: str
    git_sha: str | None
    metrics: dict[str, Any] = Field(
        description="Averages: overall, by_class, counts and latency_ms."
    )
    created_at: datetime

    @classmethod
    def build(cls, run: EvalRun) -> EvalRunSummary:
        return cls(
            id=run.id,
            suite=run.suite,
            suite_version=run.suite_version,
            retriever=run.retriever,
            config=run.config,
            repo_sha=run.repo_sha,
            git_sha=run.git_sha,
            metrics=run.metrics,
            created_at=run.created_at,
        )


class EvalRunOut(EvalRunSummary):
    per_item: list[dict[str, Any]] = Field(
        description="Each item's metrics, top hits and the gold it missed."
    )


class EvalRunPage(BaseModel):
    items: list[EvalRunSummary]
    next_cursor: uuid.UUID | None


@router.get("/evals", response_model=EvalRunPage)
async def list_eval_runs(
    _staff: CurrentStaff,
    db: DbSession,
    suite: str | None = None,
    retriever: str | None = None,
    before: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> EvalRunPage:
    """Stored eval runs, newest first (staff)."""
    query = select(EvalRun).order_by(EvalRun.id.desc()).limit(limit)
    if suite is not None:
        query = query.where(EvalRun.suite == suite)
    if retriever is not None:
        query = query.where(EvalRun.retriever == retriever)
    if before is not None:
        query = query.where(EvalRun.id < before)
    items = [EvalRunSummary.build(run) for run in await db.scalars(query)]
    return EvalRunPage(items=items, next_cursor=items[-1].id if len(items) == limit else None)


@router.get("/evals/{run}", response_model=EvalRunOut)
async def get_eval_run(_staff: CurrentStaff, db: DbSession, run: uuid.UUID) -> EvalRunOut:
    """One eval run with its per-item results (staff)."""
    found = await db.get(EvalRun, run)
    if found is None:
        raise NotFound("Eval run not found.")
    return EvalRunOut(**EvalRunSummary.build(found).model_dump(), per_item=found.per_item)
