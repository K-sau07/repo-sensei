"""Fix mode (8.3): a fix run (one issue taken from intake to a draft pull request, or to an
honest stop), the decisions people make on it, and an org's Sentry integration.

A run is its repository's org's: members see it; its ticket is what intake made of the paste,
the GitHub issue or the Sentry issue (:mod:`sensei_core.fixes.tickets`). ``stage`` is where the
pipeline is (8.4), ``status`` what that means for people: waiting for triage, going, waiting
for approval, or finished one way or another.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin
from sensei_core.db.tenancy import OrgScopedMixin


class FixSource(enum.StrEnum):
    PASTE = "paste"
    GITHUB_ISSUE = "github_issue"
    SENTRY = "sentry"
    REQUEST = "request"  # a change described in words (9.1)


class FixKind(enum.StrEnum):
    """What a run does (9.1): fix a reported bug (reproduced first), or make a requested change
    (planned from the request). Both go through the same pipeline: approval, patch, verify,
    pull request."""

    FIX = "fix"
    CHANGE = "change"


class FixStatus(enum.StrEnum):
    NEEDS_TRIAGE = "needs_triage"  # in the inbox: someone decides whether to start it
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"  # a draft pull request is open
    NOT_REPRODUCED = "not_reproduced"
    BLOCKED = "blocked"  # a rule stopped it (anti-gaming, risk)
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TERMINAL = frozenset(
    {
        FixStatus.SUCCEEDED,
        FixStatus.NOT_REPRODUCED,
        FixStatus.BLOCKED,
        FixStatus.FAILED,
        FixStatus.REJECTED,
        FixStatus.CANCELLED,
    }
)


class FixStage(enum.StrEnum):
    INTAKE = "intake"
    INVESTIGATE = "investigate"
    REPRODUCE = "reproduce"
    ROOT_CAUSE = "root_cause"
    PLAN = "plan"
    AWAITING_APPROVAL = "awaiting_approval"
    PATCH = "patch"
    VERIFY = "verify"
    PR = "pr"
    DONE = "done"


class ReproResult(enum.StrEnum):
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    FLAKY = "flaky"


class FixRisk(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalDecision(enum.StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


def _values(kind: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in kind]


def _enum(kind: type[enum.StrEnum], name: str) -> Enum:
    return Enum(kind, name=name, values_callable=_values)


class FixRun(IdMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "fix_runs"
    __table_args__ = (
        Index("ix_fix_runs_repo_created", "repo_id", "kind", "created_at"),
        Index("ix_fix_runs_org_status", "org_id", "status"),
        Index("ix_fix_runs_source_ref", "org_id", "source", "source_ref"),
        # One run at a time per issue: two deliveries at once can't both start one.
        Index(
            "uq_fix_runs_open_ref",
            "repo_id",
            "source",
            "source_ref",
            unique=True,
            postgresql_where=text(
                "source_ref IS NOT NULL AND status NOT IN ('succeeded', 'not_reproduced', "
                "'blocked', 'failed', 'rejected', 'cancelled')"
            ),
        ),
        UniqueConstraint("id", "org_id"),  # for the (run, org) foreign key of approvals
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "merge_readiness IS NULL OR (merge_readiness >= 0 AND merge_readiness <= 5)",
            name="merge_readiness_range",
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    kind: Mapped[FixKind] = mapped_column(
        _enum(FixKind, "fix_kind"), default=FixKind.FIX, server_default=FixKind.FIX.value
    )
    source: Mapped[FixSource] = mapped_column(_enum(FixSource, "fix_source"))
    # The issue's URL, the Sentry issue's id: what the run came from (None for a paste).
    source_ref: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(Text)
    ticket: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The commit being fixed: the last index's when the investigation began (every later stage
    # reproduces, patches and verifies at it).
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[FixStatus] = mapped_column(_enum(FixStatus, "fix_status"))
    stage: Mapped[FixStage] = mapped_column(_enum(FixStage, "fix_stage"))
    repro_result: Mapped[ReproResult | None] = mapped_column(_enum(ReproResult, "repro_result"))
    root_cause: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The reproducing test and its runs (8.6): the patch stage writes it again, verify runs it.
    repro: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The change (8.7): its diff, files and size, the attempts and the suite's baseline.
    patch: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Its verification from a clean checkout: the runs, the verifier's verdict and extra tests,
    # risk and merge readiness with their reasons, and what couldn't be verified.
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    diff_ref: Mapped[str | None] = mapped_column(Text)  # where the patch is stored
    risk: Mapped[FixRisk | None] = mapped_column(_enum(FixRisk, "fix_risk"))
    merge_readiness: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    # Why it stopped short (not reproduced, blocked, failed, cancelled), for people.
    stop_reason: Mapped[str | None] = mapped_column(Text)
    # When its status last changed: how long it has been queued, or running (the stall sweep).
    status_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # What it has spent, against the fixer's budget.
    spent_turns: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    spent_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default="0"
    )
    spent_sandbox_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")


class FixApproval(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "fix_approvals"
    __table_args__ = (
        # The run is in the same org (row-level security checks the approval's own org only).
        ForeignKeyConstraint(
            ["fix_run_id", "org_id"], ["fix_runs.id", "fix_runs.org_id"], ondelete="CASCADE"
        ),
        # A person approves a revision of the plan once (two approvers are two people).
        Index(
            "uq_fix_approvals_one_approval",
            "fix_run_id",
            "plan_revision",
            "user_id",
            unique=True,
            postgresql_where=text("decision = 'approve'"),
        ),
    )

    fix_run_id: Mapped[uuid.UUID] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    decision: Mapped[ApprovalDecision] = mapped_column(_enum(ApprovalDecision, "fix_decision"))
    # The plan's revision decided on (8.8): approvals of an earlier plan don't count for a new one.
    plan_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    comment: Mapped[str | None] = mapped_column(Text)
    plan_edits: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SentryIntegration(IdMixin, OrgScopedMixin, TimestampMixin, Base):
    """An org's Sentry internal integration: its token and webhook secret (encrypted), which of
    its projects are which repositories, and whether busy issues start a fix by themselves."""

    __tablename__ = "sentry_integrations"
    __table_args__ = (Index("uq_sentry_integrations_org", "org_id", unique=True),)

    sentry_org: Mapped[str] = mapped_column(String(100))  # the Sentry organization's slug
    api_url: Mapped[str] = mapped_column(String(200), default="https://sentry.io")
    token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    client_secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    # Sentry project slug -> repository id.
    projects: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, server_default="{}")
    auto_run: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    min_events: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
