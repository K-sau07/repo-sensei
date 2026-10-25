"""Batches that survive a restart: each one is a row in ``llm_batches``, so a worker that stops
part way finds its batches again (:func:`uncollected`), polls them (:func:`refresh`) and collects
their results (:func:`collect`) exactly once.

Sending takes two transactions, so the provider never bills for a batch we forgot::

    async with org_session() as db:                       # commits: the row exists first
        batch = await batches.reserve(db, client, requests, org_id=..., kind="summaries")
    async with org_session() as db:                       # commits the provider's id
        await batches.send(db, client, batch.id, requests)

A row left ``submitting`` (a crash between the two, or a send that failed) is settled by
:func:`reconcile`: matched to the provider's batch it became, or marked ``failed``.

:func:`collect` claims the batch before reading its results: run it in the transaction that
stores what the results produce. A second collector (a redelivered task) finds the claim and
stops; a collection that fails part way rolls back whole, claim and usage included, and runs
again.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from sensei_core.models import LLMBatch, LLMBatchStatus
from sensei_engine.llm.anthropic_client import (
    PROVIDER,
    AnthropicClient,
    BatchItem,
    BatchState,
    LLMRequest,
    LLMUsageSink,
)

Sleep = Callable[[float], Awaitable[None]]

# How far a provider batch's creation time may be from its row's (clock skew, a slow send).
MATCH_WINDOW = timedelta(minutes=10)
# How long the provider keeps a batch's results after the batch was created: an older batch not
# collected yet never will be (the cost guard settles it at its estimate:
# sensei_engine.llm.summaries.settle_expired).
RESULTS_KEPT = timedelta(days=29)


class BatchNotCollectable(Exception):
    """The batch hasn't ended, or another collector has (or is collecting) it."""


async def reserve(
    db: AsyncSession,
    client: AnthropicClient,
    requests: Sequence[tuple[str, LLMRequest]],
    *,
    org_id: uuid.UUID,
    kind: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> LLMBatch:
    """Record a batch of ``(custom id, request)`` pairs before it is sent (commit, then
    :func:`send`). Every request uses the same model tier (a row names one model)."""
    tiers = {request.tier for _, request in requests}
    if len(tiers) != 1:
        msg = f"a batch uses one model tier, got {sorted(tiers)}"
        raise ValueError(msg)
    batch = LLMBatch(
        org_id=org_id,
        kind=kind,
        provider=PROVIDER,
        model=client.model_for(tiers.pop()),
        status=LLMBatchStatus.SUBMITTING,
        request_count=len(requests),
        ref_type=ref_type,
        ref_id=ref_id,
    )
    db.add(batch)
    await db.flush()
    return batch


async def send(
    db: AsyncSession,
    client: AnthropicClient,
    batch_id: uuid.UUID,
    requests: Sequence[tuple[str, LLMRequest]],
) -> LLMBatch:
    """Send a reserved batch and record the provider's id. The row is locked while it is sent,
    so it goes out once."""
    batch = await db.scalar(select(LLMBatch).where(LLMBatch.id == batch_id).with_for_update())
    if batch is None or batch.status is not LLMBatchStatus.SUBMITTING:
        msg = f"batch {batch_id} isn't waiting to be sent"
        raise ValueError(msg)
    if batch.provider_batch_id is not None or len(requests) != batch.request_count:
        msg = f"batch {batch_id} was reserved for {batch.request_count} requests"
        raise ValueError(msg)
    _apply(batch, await client.submit_batch(requests))
    await db.flush()
    return batch


async def reconcile(
    db: AsyncSession,
    client: AnthropicClient,
    *,
    older_than: timedelta = MATCH_WINDOW,
    now: datetime | None = None,
) -> list[LLMBatch]:
    """Settle the rows still ``submitting`` after ``older_than``: each takes the provider batch
    created around its time with its number of requests (and no row of its own), or is marked
    ``failed`` when there isn't exactly one. Returns the rows it settled."""
    now = now or datetime.now(UTC)
    stuck = list(
        (
            await db.scalars(
                select(LLMBatch)
                .where(
                    LLMBatch.status == LLMBatchStatus.SUBMITTING,
                    LLMBatch.created_at < now - older_than,
                )
                .order_by(LLMBatch.created_at)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if not stuck:
        return []
    provider = await client.recent_batches(since=stuck[0].created_at - MATCH_WINDOW)
    known = set(
        (
            await db.scalars(
                select(LLMBatch.provider_batch_id).where(
                    LLMBatch.provider_batch_id.in_([b.id for b in provider])
                )
            )
        ).all()
    )
    for batch in stuck:
        candidates = [
            state
            for state in provider
            if state.id not in known
            and state.request_count == batch.request_count
            and abs(state.created_at - batch.created_at) <= MATCH_WINDOW
        ]
        if len(candidates) == 1:
            _apply(batch, candidates[0])
            known.add(candidates[0].id)
        else:
            batch.status = LLMBatchStatus.FAILED
    await db.flush()
    return stuck


async def refresh(db: AsyncSession, client: AnthropicClient, batch: LLMBatch) -> LLMBatch:
    """Ask the provider how far ``batch`` got and record it."""
    if batch.provider_batch_id is None:
        msg = f"batch {batch.id} hasn't been sent"
        raise ValueError(msg)
    _apply(batch, await client.batch_state(batch.provider_batch_id))
    await db.flush()
    return batch


async def wait(
    db: AsyncSession,
    client: AnthropicClient,
    batch: LLMBatch,
    *,
    poll_seconds: float,
    sleep: Sleep = asyncio.sleep,
) -> LLMBatch:
    """Poll ``batch`` every ``poll_seconds`` until it ends (most end within an hour, all within
    24). A worker that shouldn't hold a slot that long schedules :func:`refresh` instead."""
    while (await refresh(db, client, batch)).status is not LLMBatchStatus.ENDED:
        await sleep(poll_seconds)
    return batch


async def collect(
    db: AsyncSession,
    client: AnthropicClient,
    batch: LLMBatch,
    *,
    json_output: bool = False,
    on_usage: LLMUsageSink | None = None,
) -> list[BatchItem]:
    """Claim an ended batch for this transaction and return all its results, each answer's
    usage reported. Raises :class:`BatchNotCollectable` if the batch hasn't ended or is
    claimed already (a concurrent collector waits for the first to commit or roll back)."""
    claimed = await db.scalar(
        update(LLMBatch)
        .where(
            LLMBatch.id == batch.id,
            LLMBatch.status == LLMBatchStatus.ENDED,
            LLMBatch.collected_at.is_(None),
        )
        .values(collected_at=func.now())
        .returning(LLMBatch.collected_at)
    )
    if claimed is None or batch.provider_batch_id is None:
        msg = f"batch {batch.id} hasn't ended or was collected already"
        raise BatchNotCollectable(msg)
    set_committed_value(batch, "collected_at", claimed)
    results = client.batch_results(batch.provider_batch_id, json_output=json_output)
    items = [item async for item in results]
    if on_usage is not None:
        for item in items:
            for usage in item.usage:
                await on_usage(usage)
    return items


async def uncollected(
    db: AsyncSession, *, kind: str, ref_type: str | None = None, ref_id: str | None = None
) -> list[LLMBatch]:
    """Batches of ``kind`` (for ``ref_type``/``ref_id``, when given) whose results haven't been
    collected yet, oldest first: waiting to be sent, running, or ended and waiting."""
    query = select(LLMBatch).where(
        LLMBatch.kind == kind,
        LLMBatch.collected_at.is_(None),
        LLMBatch.status != LLMBatchStatus.FAILED,
    )
    if ref_type is not None:
        query = query.where(LLMBatch.ref_type == ref_type)
    if ref_id is not None:
        query = query.where(LLMBatch.ref_id == ref_id)
    return list((await db.scalars(query.order_by(LLMBatch.created_at, LLMBatch.id))).all())


def _apply(batch: LLMBatch, state: BatchState) -> None:
    batch.provider_batch_id = state.id
    batch.status = LLMBatchStatus(state.status)
    batch.succeeded, batch.errored = state.succeeded, state.errored
    batch.canceled, batch.expired = state.canceled, state.expired
    if batch.status is LLMBatchStatus.ENDED and batch.ended_at is None:
        batch.ended_at = datetime.now(UTC)
