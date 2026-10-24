"""Runs in the ``eval_runs`` table. Written through the system role (the app role may only read)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import EvalRun
from sensei_evals.runner import ItemResult, RunRecord


def to_row(run: RunRecord) -> EvalRun:
    return EvalRun(
        id=run.id,
        suite=run.suite,
        suite_version=run.suite_version,
        retriever=run.retriever,
        config=run.config,
        repo_sha=run.repo_sha,
        metrics=run.metrics,
        per_item=[item.model_dump(mode="json") for item in run.per_item],
        created_at=run.created_at,
        git_sha=run.git_sha,
    )


def from_row(row: EvalRun) -> RunRecord:
    return RunRecord(
        id=row.id,
        suite=row.suite,
        suite_version=row.suite_version,
        retriever=row.retriever,
        config=row.config,
        repo_sha=row.repo_sha,
        metrics=row.metrics,
        per_item=[ItemResult.model_validate(item) for item in row.per_item],
        created_at=row.created_at,
        git_sha=row.git_sha,
    )


async def save_run(session: AsyncSession, run: RunRecord) -> None:
    session.add(to_row(run))
    await session.flush()


async def load_run(session: AsyncSession, run_id: uuid.UUID) -> RunRecord | None:
    row = await session.get(EvalRun, run_id)
    return from_row(row) if row is not None else None
