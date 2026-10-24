# pyright: basic
"""Glossary terms per snapshot (row-level security on org_id), and the estimated cost of an LLM
batch, recorded by the cost guard before it is sent.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-24 16:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "glossary_terms",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("symbol_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_glossary_terms_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_glossary_terms_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "org_id"],
            ["snapshots.id", "snapshots.org_id"],
            name=op.f("fk_glossary_terms_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_glossary_terms")),
        sa.UniqueConstraint("snapshot_id", "term", name=op.f("uq_glossary_terms_snapshot_id_term")),
    )
    op.create_index(op.f("ix_glossary_terms_org_id"), "glossary_terms", ["org_id"], unique=False)
    op.create_index("ix_glossary_terms_repo_id", "glossary_terms", ["repo_id"], unique=False)
    for statement in enable_org_rls_sql("glossary_terms"):
        op.execute(statement)
    op.add_column(
        "llm_batches",
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_batches", "estimated_cost_usd")
    for statement in disable_org_rls_sql("glossary_terms"):
        op.execute(statement)
    op.drop_index("ix_glossary_terms_repo_id", table_name="glossary_terms")
    op.drop_index(op.f("ix_glossary_terms_org_id"), table_name="glossary_terms")
    op.drop_table("glossary_terms")
