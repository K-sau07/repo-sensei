"""Snapshots (what was indexed) and index jobs (how far indexing got)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin
from sensei_core.db.tenancy import OrgScopedMixin


def _values(enum_type: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class SnapshotStatus(enum.StrEnum):
    PENDING = "pending"  # being cloned and archived
    READY = "ready"
    FAILED = "failed"


class JobKind(enum.StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(enum.StrEnum):
    """Indexing stages, in the order they run."""

    CLONE = "clone"
    DISCOVER = "discover"
    MERKLE = "merkle"
    PARSE = "parse"
    SYMBOLS = "symbols"
    CHUNKS = "chunks"
    EMBEDDINGS = "embeddings"
    SUMMARIES = "summaries"
    GRAPH = "graph"
    LINKS = "links"
    FINALIZE = "finalize"


ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class Snapshot(IdMixin, OrgScopedMixin, Base):
    """One commit of a repository, archived to object storage. Unique per (repo, commit)."""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("repo_id", "commit_sha"),
        UniqueConstraint("id", "org_id"),
        # (repo, org) together: a snapshot can't belong to another org's repository.
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    ref: Mapped[str] = mapped_column(String(255))  # the branch or tag it was taken from
    commit_sha: Mapped[str] = mapped_column(String(64))
    status: Mapped[SnapshotStatus] = mapped_column(
        Enum(SnapshotStatus, name="snapshot_status", values_callable=_values),
        default=SnapshotStatus.PENDING,
        server_default=SnapshotStatus.PENDING.value,
    )
    file_count: Mapped[int | None] = mapped_column(Integer)
    loc: Mapped[int | None] = mapped_column(BigInteger)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    merkle_root: Mapped[str | None] = mapped_column(String(64))
    # Symbols extracted from the snapshot (None: not extracted yet; 0: none found).
    symbol_count: Mapped[int | None] = mapped_column(Integer)
    # Chunks saved for the snapshot (None: not chunked yet), for keyword and semantic search:
    # the live generation's count when it went live. A reindex of the same commit that drops
    # files deletes their chunks at once, so until its own generation goes live this can count
    # more than are left (a figure to show, not one to page by).
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    # The generation of chunks search reads (None: none live); set with ``chunk_count``.
    chunk_generation: Mapped[int | None] = mapped_column(BigInteger)
    # What discovery found: totals per language, role and flag, frameworks, skipped paths.
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexJob(IdMixin, OrgScopedMixin, Base):
    """One indexing run. ``stages`` holds per-stage progress::

        {"parse": {"status": "running", "done": 120, "total": 900, "rate": 14.2,
                   "started_at": "...", "finished_at": null, "error": null}, ...}

    A repository has at most one queued or running job (partial unique index)."""

    __tablename__ = "index_jobs"
    __table_args__ = (
        Index(
            "uq_index_jobs_one_active_per_repo",
            "repo_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_index_jobs_repo_id_created_at", "repo_id", "created_at"),
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL")
    )
    kind: Mapped[JobKind] = mapped_column(
        Enum(JobKind, name="index_job_kind", values_callable=_values)
    )
    # The branch and commit a push named (5.1); null: the default branch's head when it runs.
    ref: Mapped[str | None] = mapped_column(String(255))
    target_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="index_job_status", values_callable=_values),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )
    stage: Mapped[Stage | None] = mapped_column(
        Enum(Stage, name="index_stage", values_callable=_values)
    )
    stages: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    progress: Mapped[float] = mapped_column(default=0.0, server_default="0")
    eta_s: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default="0"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
