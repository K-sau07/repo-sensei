"""LLM usage in the usage ledger, priced per model (batch usage at the batch discount)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import UsageKind
from sensei_core.usage import CostCalculator, record_usage
from sensei_engine.llm.anthropic_client import LLMUsage, LLMUsageSink


def usage_ledger(
    db: AsyncSession,
    calculator: CostCalculator,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> LLMUsageSink:
    """A sink that records each model's usage in the ledger, priced (the session must be scoped
    to ``org_id``)."""

    async def record(usage: LLMUsage) -> None:
        await record_usage(
            db,
            calculator,
            org_id=org_id,
            kind=UsageKind.LLM,
            provider=usage.provider,
            model=usage.model,
            user_id=user_id,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            tokens_cache_read=usage.cache_read_tokens,
            tokens_cache_write=usage.cache_write_tokens,
            batch=usage.batch,
            ref_type=ref_type,
            ref_id=ref_id,
        )

    return record
