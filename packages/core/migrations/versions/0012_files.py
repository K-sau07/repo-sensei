# pyright: basic
"""Discovered files per snapshot (row-level security on org_id), and snapshot summaries.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20 17:08:17.474120+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_sha", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("loc", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("flags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("framework_root", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_files_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_files_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_files_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_files")),
        sa.UniqueConstraint("snapshot_id", "path", name=op.f("uq_files_snapshot_id_path")),
    )
    op.create_index(op.f("ix_files_org_id"), "files", ["org_id"], unique=False)
    op.create_index("ix_files_repo_id", "files", ["repo_id"], unique=False)
    for statement in enable_org_rls_sql("files"):
        op.execute(statement)
    op.add_column(
        "snapshots", sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("snapshots", "summary")
    for statement in disable_org_rls_sql("files"):
        op.execute(statement)
    op.drop_index("ix_files_repo_id", table_name="files")
    op.drop_index(op.f("ix_files_org_id"), table_name="files")
    op.drop_table("files")
