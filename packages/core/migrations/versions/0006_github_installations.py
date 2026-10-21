# pyright: basic
"""GitHub App installations linked to orgs (row-level security on org_id).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19 17:37:30.274382+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_installations",
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(length=100), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_selection", sa.String(length=16), nullable=False),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "events",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("linked_by", sa.Uuid(), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
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
            ["linked_by"],
            ["users.id"],
            name=op.f("fk_github_installations_linked_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_github_installations_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_installations")),
        sa.UniqueConstraint(
            "installation_id", name=op.f("uq_github_installations_installation_id")
        ),
    )
    op.create_index(
        op.f("ix_github_installations_org_id"), "github_installations", ["org_id"], unique=False
    )

    for statement in enable_org_rls_sql("github_installations"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_org_rls_sql("github_installations"):
        op.execute(statement)
    op.drop_index(op.f("ix_github_installations_org_id"), table_name="github_installations")
    op.drop_table("github_installations")
