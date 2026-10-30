# pyright: basic
"""Feature maps (4.9): a module's map at a commit (its purpose, entry points, key symbols, data
flow and citations, with its files' content hashes); row-level security on org_id.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-30 17:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_maps",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("entry_points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("key_symbols", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("data_flow", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "file_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("stale", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_feature_maps_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_feature_maps_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "org_id"],
            ["snapshots.id", "snapshots.org_id"],
            name=op.f("fk_feature_maps_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_maps")),
        sa.UniqueConstraint("snapshot_id", "key", name=op.f("uq_feature_maps_snapshot_id_key")),
    )
    op.create_index(op.f("ix_feature_maps_org_id"), "feature_maps", ["org_id"], unique=False)
    op.create_index("ix_feature_maps_repo_id", "feature_maps", ["repo_id"], unique=False)
    for statement in enable_org_rls_sql("feature_maps"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_org_rls_sql("feature_maps"):
        op.execute(statement)
    op.drop_index("ix_feature_maps_repo_id", table_name="feature_maps")
    op.drop_index(op.f("ix_feature_maps_org_id"), table_name="feature_maps")
    op.drop_table("feature_maps")
