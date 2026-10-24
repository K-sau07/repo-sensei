# pyright: basic
"""Eval runs: a system table; the app role may only read it (staff routes).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-22 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import read_only_sql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("suite", sa.String(length=200), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=False),
        sa.Column("retriever", sa.String(length=200), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("repo_sha", sa.String(length=40), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("per_item", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("git_sha", sa.String(length=40), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_runs")),
    )
    op.create_index(op.f("ix_eval_runs_suite"), "eval_runs", ["suite"], unique=False)
    for statement in read_only_sql("eval_runs"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(op.f("ix_eval_runs_suite"), table_name="eval_runs")
    op.drop_table("eval_runs")
