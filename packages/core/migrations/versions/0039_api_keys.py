# pyright: basic
"""API keys (7.1): ``api_keys``, a member's key for one org (its prefix and an argon2id hash of
the key, its scopes, when it was last used, expires, was revoked). Row-level security on
org_id, plus the ``key_by_prefix`` policy that lets a bearer key be found before its org is
known.

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-01 18:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import api_keys_rls_sql, disable_org_rls_sql

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("char_length(prefix) = 8", name=op.f("ck_api_keys_prefix_length")),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_api_keys_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_api_keys_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )
    op.create_index(op.f("ix_api_keys_org_id"), "api_keys", ["org_id"], unique=False)
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"], unique=False)
    for statement in api_keys_rls_sql("api_keys"):
        op.execute(statement)


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS key_by_prefix ON "api_keys"')
    for statement in disable_org_rls_sql("api_keys"):
        op.execute(statement)
    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_org_id"), table_name="api_keys")
    op.drop_table("api_keys")
