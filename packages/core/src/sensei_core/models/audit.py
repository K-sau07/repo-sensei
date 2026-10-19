"""Append-only records: the audit log and the usage (cost) ledger."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin


class ActorType(enum.StrEnum):
    USER = "user"
    SYSTEM = "system"
    API_KEY = "api_key"


class UsageKind(enum.StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    INDEX_MINUTES = "index_minutes"
    SANDBOX_SECONDS = "sandbox_seconds"


def _values(members: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in members]


class AuditEvent(IdMixin, Base):
    """Who did what, to what, when. ``org_id`` is null for account-level events (sign-in)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index(
            "ix_audit_log_org_id_created_at",
            "org_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="audit_actor_type", values_callable=_values)
    )
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ip: Mapped[str | None] = mapped_column(INET)
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class UsageEvent(IdMixin, Base):
    """One billable unit of work (an LLM call, an embedding batch, sandbox time...)."""

    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_events_org_id_created_at", "org_id", "created_at"),)

    # Token counts follow the provider's usage report; cache reads and writes are priced apart.

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[UsageKind] = mapped_column(
        Enum(UsageKind, name="usage_kind", values_callable=_values)
    )
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    tokens_cache_read: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    tokens_cache_write: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    units: Mapped[Decimal] = mapped_column(Numeric(14, 4), server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    price_version: Mapped[str] = mapped_column(String(32))
    batch: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
