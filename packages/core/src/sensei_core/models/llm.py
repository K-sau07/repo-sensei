"""LLM batches: requests sent to a provider's batch API, tracked so a restarted worker picks up
where it left off (polls the batches still running, collects the results of those that ended).

A row is written (and committed) before the batch is sent, so a batch the provider accepted is
never forgotten: a row still ``submitting`` after a crash is matched to the provider's list of
batches, or marked ``failed`` if it was never sent."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, Index, Integer, Numeric, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin
from sensei_core.db.tenancy import OrgScopedMixin


def _values(enum_type: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class LLMBatchStatus(enum.StrEnum):
    """The provider's processing status (Anthropic's ``processing_status``), plus ours before and
    instead of it."""

    SUBMITTING = "submitting"  # recorded, not yet known to be accepted by the provider
    FAILED = "failed"  # never accepted (refused, or lost before it was sent)
    IN_PROGRESS = "in_progress"
    CANCELING = "canceling"
    ENDED = "ended"  # every request has a result (succeeded, errored, canceled or expired)


class LLMBatch(IdMixin, OrgScopedMixin, Base):
    """One provider batch. ``collected_at`` is set once its results have been handled; a batch
    that ended without it still has results to collect."""

    __tablename__ = "llm_batches"
    __table_args__ = (
        Index(
            "ix_llm_batches_uncollected",
            "kind",
            "ref_type",
            "ref_id",
            postgresql_where=text("collected_at IS NULL"),
        ),
    )

    kind: Mapped[str] = mapped_column(String(50))  # what the requests are for: "summaries"
    provider: Mapped[str] = mapped_column(String(50))
    provider_batch_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[LLMBatchStatus] = mapped_column(
        Enum(LLMBatchStatus, name="llm_batch_status", values_callable=_values),
        default=LLMBatchStatus.SUBMITTING,
        server_default=LLMBatchStatus.SUBMITTING.value,
    )
    request_count: Mapped[int] = mapped_column(Integer)
    succeeded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    errored: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    canceled: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expired: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # What the batch belongs to (an index job), as in the usage ledger.
    ref_type: Mapped[str | None] = mapped_column(String(50))
    ref_id: Mapped[str | None] = mapped_column(String(100))
    # What the cost guard expected the batch to cost before it was sent (the real cost is in
    # the usage ledger).
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
