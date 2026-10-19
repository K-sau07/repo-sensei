# pyright: basic
"""Job failures (dead letters): a system table; the app role may only insert.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19 20:17:19.446137+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import insert_only_sql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_failures",
        sa.Column("task", sa.String(length=200), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("repo_id", sa.Uuid(), nullable=True),
        sa.Column("error_type", sa.String(length=200), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_job_failures_org_id_orgs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_failures")),
    )
    op.create_index(op.f("ix_job_failures_org_id"), "job_failures", ["org_id"], unique=False)
    op.create_index(op.f("ix_job_failures_task"), "job_failures", ["task"], unique=False)
    for statement in insert_only_sql("job_failures"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(op.f("ix_job_failures_task"), table_name="job_failures")
    op.drop_index(op.f("ix_job_failures_org_id"), table_name="job_failures")
    op.drop_table("job_failures")
