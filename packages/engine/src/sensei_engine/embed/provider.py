"""What an embedding provider does: texts in, one vector per text out, in order, and what each
request cost reported as it finishes.

Voyage (:mod:`sensei_engine.embed.voyage`) is the first; another provider (a local Qwen3, say)
implements :class:`EmbeddingProvider` and drops in. Vectors of different models never mix: the
cache and the index key them by :func:`model_key` and size.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import UsageKind
from sensei_core.usage import CostCalculator, record_usage


class InputType(enum.StrEnum):
    """What the text is for: providers embed a query and a document differently (asymmetric
    retrieval), so a document's vector is never used for a query or the other way round."""

    DOCUMENT = "document"
    QUERY = "query"


@dataclass(frozen=True, slots=True)
class EmbeddingUsage:
    """One request's worth of work, as the provider billed it."""

    provider: str
    model: str
    texts: int
    tokens: int


UsageSink = Callable[[EmbeddingUsage], Awaitable[None]]


class EmbeddingProvider(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def dims(self) -> int: ...

    async def embed(
        self,
        texts: Sequence[str],
        input_type: InputType,
        *,
        on_usage: UsageSink | None = None,
    ) -> list[list[float]]:
        """One vector of :attr:`dims` floats per text, in the order given. ``on_usage`` is
        awaited once per request as it completes, one at a time (so it may write to a session),
        including for requests that finished before another failed."""
        ...


def model_key(provider: EmbeddingProvider) -> str:
    """``voyage/voyage-code-4``: how the cache and the usage ledger name a provider's model."""
    return f"{provider.provider}/{provider.model}"


def usage_ledger(
    db: AsyncSession,
    calculator: CostCalculator,
    *,
    org_id: uuid.UUID,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> UsageSink:
    """A sink that records each request in the usage ledger, priced (the session must be scoped
    to ``org_id``)."""

    async def record(usage: EmbeddingUsage) -> None:
        await record_usage(
            db,
            calculator,
            org_id=org_id,
            kind=UsageKind.EMBEDDING,
            provider=usage.provider,
            model=usage.model,
            tokens_in=usage.tokens,
            ref_type=ref_type,
            ref_id=ref_id,
        )

    return record
