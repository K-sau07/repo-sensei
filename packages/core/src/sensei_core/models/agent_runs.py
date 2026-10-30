"""Agent runs (4.8): each run of an agent (an Ask answer now; fixes, changes and diagrams
later) with what it cost and how long it took, and its steps in order (a model turn, a tool
call, a retrieval, a check), for the run timeline (4.12)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin
from sensei_core.db.tenancy import OrgScopedMixin

RUN_KINDS = ("ask", "fix", "change", "diagram")
RUN_STATUSES = ("running", "succeeded", "failed")
STEP_TYPES = ("llm", "tool", "verify", "retrieval")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AgentRun(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("id", "org_id"),  # for the (run, org) foreign key of steps
        CheckConstraint(_one_of("kind", RUN_KINDS), name="kind"),
        CheckConstraint(_one_of("status", RUN_STATUSES), name="status"),
        Index("ix_agent_runs_repo_created", "repo_id", "created_at"),
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    question: Mapped[str] = mapped_column(Text, default="", server_default="")
    model: Mapped[str] = mapped_column(String(64), default="", server_default="")
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running", server_default="running")
    stopped: Mapped[str | None] = mapped_column(String(16))  # why its answer stopped short
    turns: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cache_read: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cache_write: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default="0"
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    first_token_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentStep(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "idx"),
        CheckConstraint(_one_of("type", STEP_TYPES), name="type"),
        ForeignKeyConstraint(
            ["run_id", "org_id"], ["agent_runs.id", "agent_runs.org_id"], ondelete="CASCADE"
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column()
    idx: Mapped[int] = mapped_column(Integer)  # its place in the run, from 0
    type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    output: Mapped[Any | None] = mapped_column(JSONB)  # a small one; else in object storage
    output_ref: Mapped[str | None] = mapped_column(Text)  # its object's key
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
