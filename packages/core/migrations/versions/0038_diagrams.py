# pyright: basic
"""Diagrams (6.1): ``diagrams``, a flow drawn from a snapshot's graph (its checked spec, how much
of it the graph proves, what it was drawn from). Row-level security on org_id, plus the
``share_by_token`` policy that lets a share link read its one diagram.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-31 21:34:59.754196+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql, shared_rls_sql

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diagrams",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("sequence", "flowchart", "architecture", name="diagram_kind"),
            nullable=False,
        ),
        sa.Column("source", sa.Enum("trace", "question", name="diagram_source"), nullable=False),
        sa.Column(
            "prompt", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verified_ratio", sa.Float(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("share_token_hash", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verified_ratio >= 0 AND verified_ratio <= 1", name=op.f("ck_diagrams_verified_ratio")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_diagrams_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_diagrams_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_diagrams_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_diagrams_snapshot_id_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_diagrams")),
        sa.UniqueConstraint("share_token_hash", name=op.f("uq_diagrams_share_token_hash")),
    )
    op.create_index(op.f("ix_diagrams_org_id"), "diagrams", ["org_id"], unique=False)
    op.create_index("ix_diagrams_repo_created", "diagrams", ["repo_id", "created_at"], unique=False)
    op.create_index(op.f("ix_diagrams_snapshot_id"), "diagrams", ["snapshot_id"], unique=False)
    for statement in [*enable_org_rls_sql("diagrams"), *shared_rls_sql("diagrams")]:
        op.execute(statement)


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS share_by_token ON "diagrams"')
    for statement in disable_org_rls_sql("diagrams"):
        op.execute(statement)
    op.drop_index(op.f("ix_diagrams_snapshot_id"), table_name="diagrams")
    op.drop_index("ix_diagrams_repo_created", table_name="diagrams")
    op.drop_index(op.f("ix_diagrams_org_id"), table_name="diagrams")
    op.drop_table("diagrams")
    sa.Enum(name="diagram_source").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="diagram_kind").drop(op.get_bind(), checkfirst=True)
