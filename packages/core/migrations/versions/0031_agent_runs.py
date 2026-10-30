# pyright: basic
"""Agent runs (4.8): ``agent_runs`` (one per run of an agent: its model, turns, tokens, cost
and timings) and their ``agent_steps`` in order; row-level security on org_id for both.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-30 14:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("agent_runs", "agent_steps")


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("question", sa.Text(), server_default="", nullable=False),
        sa.Column("model", sa.String(length=64), server_default="", nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("stopped", sa.String(length=16), nullable=True),
        sa.Column("turns", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_in", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_out", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_write", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("first_token_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('ask', 'fix', 'change', 'diagram')", name=op.f("ck_agent_runs_kind")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name=op.f("ck_agent_runs_status")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_agent_runs_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_agent_runs_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_agent_runs_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
        sa.UniqueConstraint("id", "org_id", name=op.f("uq_agent_runs_id_org_id")),
    )
    op.create_index(op.f("ix_agent_runs_org_id"), "agent_runs", ["org_id"], unique=False)
    op.create_index(
        "ix_agent_runs_repo_created", "agent_runs", ["repo_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_agent_runs_user_id"), "agent_runs", ["user_id"], unique=False)
    op.create_table(
        "agent_steps",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "input", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_ref", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "type IN ('llm', 'tool', 'verify', 'retrieval')", name=op.f("ck_agent_steps_type")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_agent_steps_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "org_id"],
            ["agent_runs.id", "agent_runs.org_id"],
            name=op.f("fk_agent_steps_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_steps")),
        sa.UniqueConstraint("run_id", "idx", name=op.f("uq_agent_steps_run_id_idx")),
    )
    op.create_index(op.f("ix_agent_steps_org_id"), "agent_steps", ["org_id"], unique=False)
    for table in TABLES:
        for statement in enable_org_rls_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in reversed(TABLES):
        for statement in disable_org_rls_sql(table):
            op.execute(statement)
    op.drop_index(op.f("ix_agent_steps_org_id"), table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index(op.f("ix_agent_runs_user_id"), table_name="agent_runs")
    op.drop_index("ix_agent_runs_repo_created", table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_org_id"), table_name="agent_runs")
    op.drop_table("agent_runs")
