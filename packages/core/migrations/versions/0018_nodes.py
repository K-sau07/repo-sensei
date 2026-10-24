# pyright: basic
"""Code nodes: the symbols of each snapshot (row-level security on org_id), and how many a
snapshot has once they are extracted.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-23 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # btree_gin (trusted, like pg_trgm): the snapshot and the name's trigrams in one GIN index.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")
    op.create_unique_constraint("uq_files_id_org_id", "files", ["id", "org_id"])
    op.create_table(
        "nodes",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("symbol_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_col", sa.Integer(), nullable=False),
        sa.Column("end_col", sa.Integer(), nullable=False),
        sa.Column("lang", sa.String(length=16), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("docstring", sa.Text(), nullable=True),
        sa.Column(
            "attrs", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("owner_file_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_nodes_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_nodes_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "org_id"],
            ["snapshots.id", "snapshots.org_id"],
            name=op.f("fk_nodes_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id", "org_id"],
            ["files.id", "files.org_id"],
            name=op.f("fk_nodes_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_file_id", "org_id"],
            ["files.id", "files.org_id"],
            name=op.f("fk_nodes_owner_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_nodes")),
        sa.UniqueConstraint(
            "snapshot_id", "symbol_key", name=op.f("uq_nodes_snapshot_id_symbol_key")
        ),
    )
    op.create_index(op.f("ix_nodes_org_id"), "nodes", ["org_id"], unique=False)
    op.create_index("ix_nodes_repo_id", "nodes", ["repo_id"], unique=False)
    op.create_index(
        "ix_nodes_snapshot_lower_name",
        "nodes",
        ["snapshot_id", sa.text("lower(name) text_pattern_ops")],
        unique=False,
    )
    op.create_index(
        "ix_nodes_snapshot_reverse_qualified",
        "nodes",
        ["snapshot_id", sa.text("reverse(lower(qualified_name)) text_pattern_ops")],
        unique=False,
    )
    op.create_index(
        "ix_nodes_snapshot_name_trgm",
        "nodes",
        ["snapshot_id", "name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index("ix_nodes_file_id_lines", "nodes", ["file_id", "start_line"], unique=False)
    op.create_index("ix_nodes_owner_file_id", "nodes", ["owner_file_id"], unique=False)
    for statement in enable_org_rls_sql("nodes"):
        op.execute(statement)
    op.add_column("snapshots", sa.Column("symbol_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("snapshots", "symbol_count")
    for statement in disable_org_rls_sql("nodes"):
        op.execute(statement)
    op.drop_index("ix_nodes_owner_file_id", table_name="nodes")
    op.drop_index("ix_nodes_file_id_lines", table_name="nodes")
    op.drop_index("ix_nodes_snapshot_name_trgm", table_name="nodes", postgresql_using="gin")
    op.drop_index("ix_nodes_snapshot_reverse_qualified", table_name="nodes")
    op.drop_index("ix_nodes_snapshot_lower_name", table_name="nodes")
    op.drop_index("ix_nodes_repo_id", table_name="nodes")
    op.drop_index(op.f("ix_nodes_org_id"), table_name="nodes")
    op.drop_table("nodes")
    op.drop_constraint("uq_files_id_org_id", "files", type_="unique")
