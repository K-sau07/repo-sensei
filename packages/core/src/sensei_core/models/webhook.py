"""Webhook deliveries: the durable inbox for events from GitHub."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin


class DeliveryStatus(enum.StrEnum):
    RECEIVED = "received"  # stored, waiting for a worker
    PROCESSED = "processed"  # a handler ran
    STORED = "stored"  # kept for a later phase (push, pull_request), nothing to do yet
    IGNORED = "ignored"  # no handler for this event
    FAILED = "failed"  # the handler raised; ``error`` says why


def _status_values(statuses: type[DeliveryStatus]) -> list[str]:
    return [status.value for status in statuses]


class WebhookDelivery(IdMixin, Base):
    """One delivery, unique per provider delivery id so redeliveries are recognized.

    A system table: no org scope and no row-level security. The API role may only insert; workers
    and staff read it through the system role."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # The sweep looks only at deliveries still waiting for a worker.
        Index(
            "ix_webhook_deliveries_waiting",
            "queued_at",
            postgresql_where=text("status = 'received'"),
        ),
    )

    provider: Mapped[str] = mapped_column(String(16))
    delivery_id: Mapped[str] = mapped_column(String(64), unique=True)
    event: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))
    installation_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status", values_callable=_status_values),
        default=DeliveryStatus.RECEIVED,
        server_default=DeliveryStatus.RECEIVED.value,
    )
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When it was last handed to the queue (at intake, then by the sweep).
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
