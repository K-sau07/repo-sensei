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
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

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


SAME_KIND = (EvalRun.suite, EvalRun.suite_version, EvalRun.retriever)


async def _baseline(db: AsyncSession, run: EvalRun) -> EvalRun | None:
    """The run before ``run`` of its suite, version and retriever (by when, then id)."""
    return await db.scalar(
        select(EvalRun)
        .where(
            *(column == getattr(run, column.key) for column in SAME_KIND),
            tuple_(EvalRun.created_at, EvalRun.id)
            < tuple_(literal(run.created_at), literal(run.id)),
        )
        .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
        .limit(1)
    )


def _summarized(run: EvalRun, baseline: uuid.UUID | None, metrics: Any) -> EvalRunSummary:
    summary = EvalRunSummary.build(run)
    if baseline is not None:
        summary.baseline_id = baseline
        summary.regressions = len(metric_changes(metrics, run.metrics)[0])
    return summary


async def _summaries(db: AsyncSession, runs: list[EvalRun]) -> list[EvalRunSummary]:
    """Each run with its baseline: two queries for any number of runs (each run's previous by
    a window over its kind, then those runs' metrics alone)."""
    previous = (
        select(
            EvalRun.id,
            func.lag(EvalRun.id)
            .over(partition_by=SAME_KIND, order_by=(EvalRun.created_at, EvalRun.id))
            .label("previous"),
        )
        .where(tuple_(*SAME_KIND).in_({(r.suite, r.suite_version, r.retriever) for r in runs}))
        .subquery()
    )
    rows = await db.execute(
        select(previous.c.id, previous.c.previous).where(
            previous.c.id.in_([r.id for r in runs]), previous.c.previous.is_not(None)
        )
    )
    before: dict[uuid.UUID, uuid.UUID] = dict(rows.tuples().all())
    metrics: dict[uuid.UUID, Any] = dict(
        (await db.execute(select(EvalRun.id, EvalRun.metrics).where(
            EvalRun.id.in_(set(before.values()))
        ))).tuples().all()
    ) if before else {}  # fmt: skip
    return [
        _summarized(r, before.get(r.id), metrics.get(before[r.id]) if r.id in before else None)
        for r in runs
    ]


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
    # The items aren't listed: their JSON stays in the table.
    query = select(EvalRun).options(defer(EvalRun.per_item)).order_by(EvalRun.id.desc())
    query = query.limit(limit)
    if suite is not None:
        query = query.where(EvalRun.suite == suite)
    if retriever is not None:
        query = query.where(EvalRun.retriever == retriever)
    if before is not None:
        query = query.where(EvalRun.id < before)
    runs = list(await db.scalars(query))
    items = await _summaries(db, runs) if runs else []
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
    base = await _baseline(db, found)
    summary = _summarized(found, base.id if base else None, base.metrics if base else None)
    return EvalRunOut(**summary.model_dump(), per_item=found.per_item)


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
    summary = _summarized(found, base.id if base else None, base.metrics if base else None)
    if base is None:
        return EvalComparisonOut(
            run=summary, baseline=None, metric=None, regressions=[], improvements=[],
            unchanged=[], items_worse=[], items_better=[], warnings=[],
        )  # fmt: skip
    try:
        made = compare(from_row(base), from_row(found))
    except ValidationError:  # a row stored by hand, not by sensei-eval: its metrics still compare
        worse, better, same = metric_changes(base.metrics, found.metrics)
        return EvalComparisonOut(
            run=summary, baseline=EvalRunSummary.build(base), metric=None,
            regressions=_metrics(worse), improvements=_metrics(better), unchanged=_metrics(same),
            items_worse=[], items_better=[],
            warnings=["An item of one of the runs can't be read, so items aren't compared."],
        )  # fmt: skip

    return EvalComparisonOut(
        run=summary, baseline=EvalRunSummary.build(base), metric=made.metric,
        regressions=_metrics(made.regressions), improvements=_metrics(made.improvements),
        unchanged=_metrics(made.unchanged), items_worse=_items(made.items_worse),
        items_better=_items(made.items_better), warnings=made.warnings,
    )  # fmt: skip


def _metrics(changes: list[Any]) -> list[MetricChangeOut]:
    return [MetricChangeOut(name=c.name, before=c.before, after=c.after, delta=c.delta)
            for c in changes]  # fmt: skip


def _items(changes: list[Any]) -> list[ItemChangeOut]:
    return [ItemChangeOut(id=c.id, query=c.query, before=c.before, after=c.after)
            for c in changes]  # fmt: skip
