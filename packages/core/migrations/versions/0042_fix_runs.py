# pyright: basic
"""Fix mode (8.3): ``fix_runs`` (an issue taken from intake to a draft pull request, or to an
honest stop), ``fix_approvals`` (the decisions on it) and ``sentry_integrations`` (an org's
Sentry token and webhook secret, encrypted). Row-level security on org_id for all three.

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-02 08:47:38.428260+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("sentry_integrations", "fix_runs", "fix_approvals")
ENUMS = ("fix_source", "fix_status", "fix_stage", "repro_result", "fix_risk", "fix_decision")


def upgrade() -> None:
    op.create_table(
        "sentry_integrations",
        sa.Column("sentry_org", sa.String(length=100), nullable=False),
        sa.Column("api_url", sa.String(length=200), nullable=False),
        sa.Column("token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("client_secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column(
            "projects", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("auto_run", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("min_events", sa.Integer(), server_default="10", nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_sentry_integrations_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sentry_integrations")),
    )
    op.create_index(
        op.f("ix_sentry_integrations_org_id"), "sentry_integrations", ["org_id"], unique=False
    )
    op.create_index("uq_sentry_integrations_org", "sentry_integrations", ["org_id"], unique=True)
    op.create_table(
        "fix_runs",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source", sa.Enum("paste", "github_issue", "sentry", name="fix_source"), nullable=False
        ),
        sa.Column("source_ref", sa.String(length=500), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("ticket", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "needs_triage",
                "queued",
                "running",
                "awaiting_approval",
                "succeeded",
                "not_reproduced",
                "blocked",
                "failed",
                "rejected",
                "cancelled",
                name="fix_status",
            ),
            nullable=False,
        ),
        sa.Column(
            "stage",
            sa.Enum(
                "intake",
                "investigate",
                "reproduce",
                "root_cause",
                "plan",
                "awaiting_approval",
                "patch",
                "verify",
                "pr",
                "done",
                name="fix_stage",
            ),
            nullable=False,
        ),
        sa.Column(
            "repro_result",
            sa.Enum("reproduced", "not_reproduced", "flaky", name="repro_result"),
            nullable=True,
        ),
        sa.Column("root_cause", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("diff_ref", sa.Text(), nullable=True),
        sa.Column("risk", sa.Enum("low", "medium", "high", name="fix_risk"), nullable=True),
        sa.Column("merge_readiness", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "merge_readiness IS NULL OR (merge_readiness >= 0 AND merge_readiness <= 5)",
            name=op.f("ck_fix_runs_merge_readiness_range"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_fix_runs_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_fix_runs_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_fix_runs_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_fix_runs_run_id_agent_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fix_runs")),
    )
    op.create_index(op.f("ix_fix_runs_org_id"), "fix_runs", ["org_id"], unique=False)
    op.create_index("ix_fix_runs_org_status", "fix_runs", ["org_id", "status"], unique=False)
    op.create_index("ix_fix_runs_repo_created", "fix_runs", ["repo_id", "created_at"], unique=False)
    op.create_index(
        "ix_fix_runs_source_ref", "fix_runs", ["org_id", "source", "source_ref"], unique=False
    )
    op.create_table(
        "fix_approvals",
        sa.Column("fix_run_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum("approve", "request_changes", "reject", name="fix_decision"),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("plan_edits", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["fix_run_id"],
            ["fix_runs.id"],
            name=op.f("fk_fix_approvals_fix_run_id_fix_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_fix_approvals_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_fix_approvals_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fix_approvals")),
    )
    op.create_index(
        op.f("ix_fix_approvals_fix_run_id"), "fix_approvals", ["fix_run_id"], unique=False
    )
    op.create_index(op.f("ix_fix_approvals_org_id"), "fix_approvals", ["org_id"], unique=False)
    for table in TABLES:
        for statement in enable_org_rls_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        for statement in disable_org_rls_sql(table):
            op.execute(statement)
    op.drop_index(op.f("ix_fix_approvals_org_id"), table_name="fix_approvals")
    op.drop_index(op.f("ix_fix_approvals_fix_run_id"), table_name="fix_approvals")
    op.drop_table("fix_approvals")
    op.drop_index("ix_fix_runs_source_ref", table_name="fix_runs")
    op.drop_index("ix_fix_runs_repo_created", table_name="fix_runs")
    op.drop_index("ix_fix_runs_org_status", table_name="fix_runs")
    op.drop_index(op.f("ix_fix_runs_org_id"), table_name="fix_runs")
    op.drop_table("fix_runs")
    op.drop_index("uq_sentry_integrations_org", table_name="sentry_integrations")
    op.drop_index(op.f("ix_sentry_integrations_org_id"), table_name="sentry_integrations")
    op.drop_table("sentry_integrations")
    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
