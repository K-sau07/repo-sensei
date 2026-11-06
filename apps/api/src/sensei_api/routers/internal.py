"""Staff-only routes. Signed-in non-staff get a 404, as if they didn't exist; anonymous callers
get the usual 401.

Eval runs (10.6.2): each is compared with its baseline, the run before it of the same suite,
version and retriever (``sensei_evals.reports.compare``, as ``sensei-eval compare`` does):
which overall metrics got worse (each the right way round: cost, time and errors are better
lower) and which items did, on the score of the run's kind."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.auth import CurrentStaff
from sensei_api.deps import DbSession
from sensei_api.errors import NotFound
from sensei_core.models import EvalRun
from sensei_evals.reports import compare, metric_changes
from sensei_evals.store import from_row

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
    baseline_id: uuid.UUID | None = Field(
        default=None, description="The run before it of the same suite, version and retriever."
    )
    regressions: int | None = Field(
        default=None, description="Overall metrics worse than the baseline's (None: no baseline)."
    )

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
    next_cursor: uuid.UUID | None = Field(description="None when sorted by regressions.")


class MetricChangeOut(BaseModel):
    name: str
    before: float | None
    after: float | None
    delta: float | None


class ItemChangeOut(BaseModel):
    id: str
    query: str
    before: float | None
    after: float | None


class EvalComparisonOut(BaseModel):
    run: EvalRunSummary
    baseline: EvalRunSummary | None
    metric: str | None = Field(description="The per-item score items are compared on.")
    regressions: list[MetricChangeOut] = Field(description="The worst first.")
    improvements: list[MetricChangeOut]
    unchanged: list[MetricChangeOut]
    items_worse: list[ItemChangeOut] = Field(description="The worst first.")
    items_better: list[ItemChangeOut]
    warnings: list[str] = Field(description="Why the two runs may not be alike.")


async def _baseline(db: AsyncSession, run: EvalRun) -> EvalRun | None:
    return await db.scalar(
        select(EvalRun)
        .where(
            EvalRun.suite == run.suite,
            EvalRun.suite_version == run.suite_version,
            EvalRun.retriever == run.retriever,
            EvalRun.created_at < run.created_at,
        )
        .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
        .limit(1)
    )


async def _summary(db: AsyncSession, run: EvalRun) -> EvalRunSummary:
    summary = EvalRunSummary.build(run)
    baseline = await _baseline(db, run)
    if baseline is not None:
        summary.baseline_id = baseline.id
        summary.regressions = len(metric_changes(baseline.metrics, run.metrics)[0])
    return summary


@router.get("/evals", response_model=EvalRunPage)
async def list_eval_runs(
    *,
    _staff: CurrentStaff,
    db: DbSession,
    suite: str | None = None,
    retriever: str | None = None,
    before: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    sort: Literal["newest", "regressions"] = "newest",
) -> EvalRunPage:
    """Stored eval runs, newest first, each with its baseline and how many metrics got worse
    (staff). ``sort=regressions``: the newest ``limit`` runs, most regressions first."""
    query = select(EvalRun).order_by(EvalRun.id.desc()).limit(limit)
    if suite is not None:
        query = query.where(EvalRun.suite == suite)
    if retriever is not None:
        query = query.where(EvalRun.retriever == retriever)
    if before is not None:
        query = query.where(EvalRun.id < before)
    items = [await _summary(db, run) for run in await db.scalars(query)]
    if sort == "regressions":
        items.sort(key=lambda item: -(item.regressions or 0))  # stable: newest first among ties
        return EvalRunPage(items=items, next_cursor=None)
    return EvalRunPage(items=items, next_cursor=items[-1].id if len(items) == limit else None)


@router.get("/evals/{run}", response_model=EvalRunOut)
async def get_eval_run(_staff: CurrentStaff, db: DbSession, run: uuid.UUID) -> EvalRunOut:
    """One eval run with its per-item results (staff)."""
    found = await db.get(EvalRun, run)
    if found is None:
        raise NotFound("Eval run not found.")
    return EvalRunOut(**(await _summary(db, found)).model_dump(), per_item=found.per_item)


@router.get("/evals/{run}/compare", response_model=EvalComparisonOut)
async def compare_eval_run(
    _staff: CurrentStaff, db: DbSession, run: uuid.UUID, baseline: uuid.UUID | None = None
) -> EvalComparisonOut:
    """What changed from ``baseline`` (by default the run's own) to the run (staff)."""
    found = await db.get(EvalRun, run)
    if found is None:
        raise NotFound("Eval run not found.")
    base = await db.get(EvalRun, baseline) if baseline is not None else await _baseline(db, found)
    if baseline is not None and base is None:
        raise NotFound("Baseline run not found.")
    summary = await _summary(db, found)
    if base is None:
        return EvalComparisonOut(
            run=summary, baseline=None, metric=None, regressions=[], improvements=[],
            unchanged=[], items_worse=[], items_better=[], warnings=[],
        )  # fmt: skip
    if baseline is not None:  # another run than its own baseline: what changed from that one
        summary.baseline_id = base.id
        summary.regressions = len(metric_changes(base.metrics, found.metrics)[0])
    made = compare(from_row(base), from_row(found))

    def metrics(changes: list[Any]) -> list[MetricChangeOut]:
        return [MetricChangeOut(name=c.name, before=c.before, after=c.after, delta=c.delta)
                for c in changes]  # fmt: skip

    def items(changes: list[Any]) -> list[ItemChangeOut]:
        return [ItemChangeOut(id=c.id, query=c.query, before=c.before, after=c.after)
                for c in changes]  # fmt: skip

    return EvalComparisonOut(
        run=summary, baseline=EvalRunSummary.build(base), metric=made.metric,
        regressions=metrics(made.regressions), improvements=metrics(made.improvements),
        unchanged=metrics(made.unchanged), items_worse=items(made.items_worse),
        items_better=items(made.items_better), warnings=made.warnings,
    )  # fmt: skip
