# pyright: basic
"""Chunks: a snapshot's retrieval units, in a table hash-partitioned by repository, with BM25
indexes on each partition's search text and path text (row-level security on org_id, on the
table and on every partition); how many a snapshot has once chunked; and the system role
allowed to ANALYZE nodes and chunks after bulk loads.

The BM25 indexes are made partition by partition, not on the parent: pg_textsearch (1.5) scores
a query against an index on a partitioned table with the first partition's statistics, so
searches go to the repository's partition and its own index.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-24 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sensei_core.db.tenancy import SYSTEM_ROLE, disable_org_rls_sql, enable_org_rls_sql
from sensei_core.db.types import HalfVector

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARTITIONS = 16
MAINTAINED = ("nodes", "chunks")


def _partitions() -> list[str]:
    return [f"chunks_p{remainder:02}" for remainder in range(PARTITIONS)]


def upgrade() -> None:
    op.create_unique_constraint(op.f("uq_nodes_id_org_id"), "nodes", ["id", "org_id"])
    op.create_table(
        "chunks",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("owner_file_id", sa.Uuid(), nullable=False),
        sa.Column("symbol_id", sa.Uuid(), nullable=True),
        sa.Column("symbol_key", sa.Text(), nullable=True),
        sa.Column("view", sa.String(length=16), server_default="code", nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("lang", sa.String(length=32), nullable=True),
        sa.Column("part", sa.Integer(), nullable=True),
        sa.Column("parts", sa.Integer(), nullable=True),
        sa.Column("header", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("path_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("embedding", HalfVector(1024), nullable=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id", "org_id"],
            ["files.id", "files.org_id"],
            name=op.f("fk_chunks_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_chunks_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["owner_file_id", "org_id"],
            ["files.id", "files.org_id"],
            name=op.f("fk_chunks_owner_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_chunks_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "org_id"],
            ["snapshots.id", "snapshots.org_id"],
            name=op.f("fk_chunks_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["symbol_id", "org_id"],
            ["nodes.id", "nodes.org_id"],
            name=op.f("fk_chunks_symbol_id_nodes"),
            ondelete="SET NULL (symbol_id)",
        ),
        sa.PrimaryKeyConstraint("repo_id", "id", name=op.f("pk_chunks")),
        postgresql_partition_by="HASH (repo_id)",
    )
    for remainder, name in enumerate(_partitions()):
        op.execute(
            f"CREATE TABLE {name} PARTITION OF chunks "
            f"FOR VALUES WITH (MODULUS {PARTITIONS}, REMAINDER {remainder})"
        )
        # Keyword search reads a partition directly (see above): it isolates orgs too.
        for statement in enable_org_rls_sql(name):
            op.execute(statement)
        for column in ("search_text", "path_text"):
            op.execute(
                f"CREATE INDEX {name}_{column}_bm25 ON {name} "
                f"USING bm25 ({column}) WITH (text_config = 'simple')"
            )
    # Indexes on the parent are built on every partition (and on any partition added later).
    op.create_index(op.f("ix_chunks_org_id"), "chunks", ["org_id"], unique=False)
    op.create_index(
        "ix_chunks_repo_snapshot_view_kind",
        "chunks",
        ["repo_id", "snapshot_id", "view", "kind"],
        unique=False,
    )
    op.create_index("ix_chunks_file_id", "chunks", ["file_id"], unique=False)
    op.create_index("ix_chunks_owner_file_id", "chunks", ["owner_file_id"], unique=False)
    op.create_index("ix_chunks_symbol_id", "chunks", ["symbol_id"], unique=False)
    for statement in enable_org_rls_sql("chunks"):
        op.execute(statement)
    op.add_column("snapshots", sa.Column("chunk_count", sa.Integer(), nullable=True))
    # The worker ANALYZEs after a bulk load: until autovacuum gets there, lookups plan without
    # statistics (a prefix lookup on langflow's nodes: 61 ms instead of 1.4 ms).
    tables = ", ".join([*MAINTAINED, *_partitions()])
    op.execute(f"GRANT MAINTAIN ON TABLE {tables} TO {SYSTEM_ROLE}")


def downgrade() -> None:
    tables = ", ".join([*MAINTAINED, *_partitions()])
    op.execute(f"REVOKE MAINTAIN ON TABLE {tables} FROM {SYSTEM_ROLE}")
    op.drop_column("snapshots", "chunk_count")
    for statement in disable_org_rls_sql("chunks"):
        op.execute(statement)
    op.drop_table("chunks")  # and its partitions and indexes
    op.drop_constraint(op.f("uq_nodes_id_org_id"), "nodes", type_="unique")
