"""Diagrams (6.1): a flow drawn from the graph at one snapshot, its spec (the nodes and edges the
validator kept, each node with its citation, each edge verified or inferred with a reason) and
how much of it the graph proves. The org's: any member of a repository's org lists its
diagrams; a share link opens one read-only."""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin
from sensei_core.db.tenancy import OrgScopedMixin


class DiagramKind(enum.StrEnum):
    SEQUENCE = "sequence"
    FLOWCHART = "flowchart"
    ARCHITECTURE = "architecture"


class DiagramSource(enum.StrEnum):
    TRACE = "trace"  # a trace's path
    QUESTION = "question"  # a question: its trace, or a walk from what it names


def _values(kind: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in kind]


class Diagram(IdMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "diagrams"
    __table_args__ = (
        Index("ix_diagrams_repo_created", "repo_id", "created_at"),
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        CheckConstraint("verified_ratio >= 0 AND verified_ratio <= 1", name="verified_ratio"),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), index=True
    )
    commit_sha: Mapped[str] = mapped_column(String(64))  # kept when the snapshot goes
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[DiagramKind] = mapped_column(
        Enum(DiagramKind, name="diagram_kind", values_callable=_values)
    )
    source: Mapped[DiagramSource] = mapped_column(
        Enum(DiagramSource, name="diagram_source", values_callable=_values)
    )
    # What it was drawn from: the trace's entry (and its options) or the question.
    prompt: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    verified_ratio: Mapped[float] = mapped_column(Float)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    share_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
