# pyright: basic
"""LLM batches: provider batch jobs and how far each got (row-level security on org_id).

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-24 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = postgresql.ENUM(
    "in_progress", "canceling", "ended", name="llm_batch_status", create_type=False
)


def upgrade() -> None:
    STATUS.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "llm_batches",
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_batch_id", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("status", STATUS, server_default="in_progress", nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errored", sa.Integer(), server_default="0", nullable=False),
        sa.Column("canceled", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expired", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ref_type", sa.String(length=50), nullable=True),
        sa.Column("ref_id", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_llm_batches_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_batches")),
        sa.UniqueConstraint("provider_batch_id", name=op.f("uq_llm_batches_provider_batch_id")),
    )
    op.create_index(op.f("ix_llm_batches_org_id"), "llm_batches", ["org_id"], unique=False)
    op.create_index(
        "ix_llm_batches_uncollected",
        "llm_batches",
        ["kind", "ref_type", "ref_id"],
        unique=False,
        postgresql_where=sa.text("collected_at IS NULL"),
    )
    for statement in enable_org_rls_sql("llm_batches"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_org_rls_sql("llm_batches"):
        op.execute(statement)
    op.drop_index(
        "ix_llm_batches_uncollected",
        table_name="llm_batches",
        postgresql_where=sa.text("collected_at IS NULL"),
    )
    op.drop_index(op.f("ix_llm_batches_org_id"), table_name="llm_batches")
    op.drop_table("llm_batches")
    STATUS.drop(op.get_bind(), checkfirst=True)
