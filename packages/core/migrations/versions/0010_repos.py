# pyright: basic
"""Repositories connected to orgs (row-level security on org_id).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19 21:22:26.736017+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


REPO_SOURCE = postgresql.ENUM("github_app", "public_url", name="repo_source", create_type=False)
REPO_STATUS = postgresql.ENUM(
    "pending",
    "indexing",
    "ready",
    "error",
    "access_lost",
    "archived",
    name="repo_status",
    create_type=False,
)


def upgrade() -> None:
    REPO_SOURCE.create(op.get_bind(), checkfirst=True)
    REPO_STATUS.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "repos",
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("source", REPO_SOURCE, nullable=False),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("full_name", sa.String(length=201), nullable=False),
        sa.Column("slug", sa.String(length=201), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("size_kb", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "languages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            REPO_STATUS,
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_indexed_sha", sa.String(length=64), nullable=True),
        sa.Column("connected_by", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["connected_by"],
            ["users.id"],
            name=op.f("fk_repos_connected_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["github_installations.id"],
            name=op.f("fk_repos_installation_id_github_installations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_repos_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repos")),
        sa.UniqueConstraint(
            "org_id", "github_repo_id", name=op.f("uq_repos_org_id_github_repo_id")
        ),
    )
    op.create_index(op.f("ix_repos_installation_id"), "repos", ["installation_id"], unique=False)
    op.create_index(op.f("ix_repos_org_id"), "repos", ["org_id"], unique=False)
    op.create_index(
        "uq_repos_org_id_slug_live",
        "repos",
        ["org_id", "slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    for statement in enable_org_rls_sql("repos"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_org_rls_sql("repos"):
        op.execute(statement)
    op.drop_index(
        "uq_repos_org_id_slug_live",
        table_name="repos",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_repos_org_id"), table_name="repos")
    op.drop_index(op.f("ix_repos_installation_id"), table_name="repos")
    op.drop_table("repos")
    REPO_STATUS.drop(op.get_bind(), checkfirst=True)
    REPO_SOURCE.drop(op.get_bind(), checkfirst=True)
