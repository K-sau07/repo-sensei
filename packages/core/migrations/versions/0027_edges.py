# pyright: basic
"""The code graph (3.1): ``edges`` between a snapshot's nodes (typed, with a confidence, how it is
known and the line that proves it, owned by the file that produced it; row-level security on
org_id), and graph kinds for ``nodes``: a kind is any of :class:`NodeKind`'s (checked), and a node
may be in no file (a package, an external symbol).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-26 20:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# As of this revision (the enums in sensei_core.models.graph may grow later revisions).
NODE_KINDS = (
    "module", "class", "function", "method", "component", "hook", "variable",
    "service", "package", "external_symbol",
    "http_client_call", "api_endpoint", "graphql_operation", "graphql_field", "rpc_method",
    "topic", "orm_model", "table", "column", "sql_statement", "env_var", "config", "test",
)  # fmt: skip
EDGE_TYPES = (
    "contains", "defines", "imports", "calls", "references", "inherits", "implements",
    "renders", "uses_hook", "injects", "issues_request", "targets", "handled_by", "resolves",
    "publishes", "consumes", "maps_to", "reads", "writes", "tests",
)  # fmt: skip
PROVENANCES = (
    "scip", "lsp", "runtime", "contract", "import_resolved", "receiver_heuristic",
    "url_literal", "name_unique", "name_ambiguous", "llm_inferred",
)  # fmt: skip


def upgrade() -> None:
    op.alter_column("nodes", "kind", type_=sa.String(length=32), existing_nullable=False)
    kinds = ", ".join(f"'{kind}'" for kind in NODE_KINDS)
    op.create_check_constraint(op.f("ck_nodes_kind"), "nodes", f"kind IN ({kinds})")
    op.alter_column("nodes", "file_id", nullable=True)
    op.alter_column("nodes", "owner_file_id", nullable=True)

    edge_type = postgresql.ENUM(*EDGE_TYPES, name="edge_type", create_type=False)
    provenance = postgresql.ENUM(*PROVENANCES, name="edge_provenance", create_type=False)
    edge_type.create(op.get_bind())
    provenance.create(op.get_bind())
    op.create_table(
        "edges",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("src_id", sa.Uuid(), nullable=False),
        sa.Column("dst_id", sa.Uuid(), nullable=False),
        sa.Column("type", edge_type, nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("provenance", provenance, nullable=False),
        sa.Column("evidence_file_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_line", sa.Integer(), nullable=True),
        sa.Column("owner_file_id", sa.Uuid(), nullable=True),
        sa.Column(
            "attrs", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_edges_confidence_range")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_edges_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_edges_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "org_id"],
            ["snapshots.id", "snapshots.org_id"],
            name=op.f("fk_edges_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["src_id", "org_id"],
            ["nodes.id", "nodes.org_id"],
            name=op.f("fk_edges_src_id_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dst_id", "org_id"],
            ["nodes.id", "nodes.org_id"],
            name=op.f("fk_edges_dst_id_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_file_id", "org_id"],
            ["files.id", "files.org_id"],
            name=op.f("fk_edges_evidence_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_file_id", "org_id"],
            ["files.id", "files.org_id"],
            name=op.f("fk_edges_owner_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edges")),
    )
    op.create_index(op.f("ix_edges_org_id"), "edges", ["org_id"], unique=False)
    op.create_index("ix_edges_repo_id", "edges", ["repo_id"], unique=False)
    op.create_index("ix_edges_src_id_type", "edges", ["src_id", "type"], unique=False)
    op.create_index("ix_edges_dst_id_type", "edges", ["dst_id", "type"], unique=False)
    op.create_index("ix_edges_owner_file_id", "edges", ["owner_file_id"], unique=False)
    op.create_index(
        "uq_edges_snapshot_src_dst_type_line",
        "edges",
        ["snapshot_id", "src_id", "dst_id", "type", "evidence_line"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    for statement in enable_org_rls_sql("edges"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_org_rls_sql("edges"):
        op.execute(statement)
    op.drop_table("edges")
    op.execute("DROP TYPE edge_provenance")
    op.execute("DROP TYPE edge_type")
    # Back to definitions only: the graph's nodes go (none in no file remain). Row-level
    # security off, so a role it would filter fails here instead of deleting nothing.
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DELETE FROM nodes WHERE kind NOT IN "
        "('module', 'class', 'function', 'method', 'component', 'hook', 'variable')"
    )
    op.alter_column("nodes", "owner_file_id", nullable=False)
    op.alter_column("nodes", "file_id", nullable=False)
    op.drop_constraint(op.f("ck_nodes_kind"), "nodes", type_="check")
    op.alter_column("nodes", "kind", type_=sa.String(length=16), existing_nullable=False)
