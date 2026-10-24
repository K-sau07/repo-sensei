"""Batches that survive a restart: each one sent is a row in ``llm_batches``, so a worker that
stops while a batch runs finds it again (:func:`uncollected`), polls it (:func:`refresh`) and
collects its results (:func:`collect`) exactly once.

Commit right after :func:`submit`: the provider bills a batch whether or not we remember it.
Collect in the transaction that stores what the results produce: the usage is recorded in it
too, so a collection that fails part way rolls back whole and runs again.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import LLMBatch, LLMBatchStatus
from sensei_engine.llm.anthropic_client import (
    PROVIDER,
    AnthropicClient,
    BatchItem,
    LLMRequest,
    LLMUsageSink,
)

Sleep = Callable[[float], Awaitable[None]]


async def submit(
    db: AsyncSession,
    client: AnthropicClient,
    requests: Sequence[tuple[str, LLMRequest]],
    *,
    org_id: uuid.UUID,
    kind: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> LLMBatch:
    """Send ``(custom id, request)`` pairs as one batch and record it. Every request uses the
    same model tier (a batch row names one model)."""
    tiers = {request.tier for _, request in requests}
    if len(tiers) > 1:
        msg = f"a batch uses one model tier, got {sorted(tiers)}"
        raise ValueError(msg)
    state = await client.submit_batch(requests)
    batch = LLMBatch(
        org_id=org_id,
        kind=kind,
        provider=PROVIDER,
        provider_batch_id=state.id,
        model=client.model_for(tiers.pop()),
        status=LLMBatchStatus(state.status),
        request_count=len(requests),
        ref_type=ref_type,
        ref_id=ref_id,
    )
    db.add(batch)
    await db.flush()
    return batch


async def refresh(db: AsyncSession, client: AnthropicClient, batch: LLMBatch) -> LLMBatch:
    """Ask the provider how far ``batch`` got and record it."""
    state = await client.batch_state(batch.provider_batch_id)
    batch.status = LLMBatchStatus(state.status)
    batch.succeeded, batch.errored = state.succeeded, state.errored
    batch.canceled, batch.expired = state.canceled, state.expired
    if batch.status is LLMBatchStatus.ENDED and batch.ended_at is None:
        batch.ended_at = datetime.now(UTC)
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
) -> AsyncIterator[BatchItem]:
    """The results of an ended batch, each answer's usage reported as it comes; the batch is
    marked collected once all have been read."""
    if batch.status is not LLMBatchStatus.ENDED:
        msg = f"batch {batch.provider_batch_id} hasn't ended ({batch.status})"
        raise ValueError(msg)
    if batch.collected_at is not None:
        msg = f"batch {batch.provider_batch_id} was already collected"
        raise ValueError(msg)
    async for item in client.batch_results(batch.provider_batch_id, json_output=json_output):
        if on_usage is not None:
            for usage in item.usage:
                await on_usage(usage)
        yield item
    batch.collected_at = datetime.now(UTC)
    await db.flush()


async def uncollected(
    db: AsyncSession, *, kind: str, ref_type: str | None = None, ref_id: str | None = None
) -> list[LLMBatch]:
    """Batches of ``kind`` (for ``ref_type``/``ref_id``, when given) whose results haven't been
    collected yet, oldest first: still running, or ended and waiting."""
    query = select(LLMBatch).where(LLMBatch.kind == kind, LLMBatch.collected_at.is_(None))
    if ref_type is not None:
        query = query.where(LLMBatch.ref_type == ref_type)
    if ref_id is not None:
        query = query.where(LLMBatch.ref_id == ref_id)
    return list((await db.scalars(query.order_by(LLMBatch.created_at, LLMBatch.id))).all())
