# pyright: basic
"""Audit log and usage ledger: append-only for the app role, with row-level security.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18 23:19:39.676625+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import (
    append_only_sql,
    audit_log_rls_sql,
    disable_org_rls_sql,
    enable_org_rls_sql,
)

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACTOR_TYPE = postgresql.ENUM(
    "user", "system", "api_key", name="audit_actor_type", create_type=False
)
USAGE_KIND = postgresql.ENUM(
    "llm",
    "embedding",
    "rerank",
    "index_minutes",
    "sandbox_seconds",
    name="usage_kind",
    create_type=False,
)


def upgrade() -> None:
    ACTOR_TYPE.create(op.get_bind(), checkfirst=True)
    USAGE_KIND.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "audit_log",
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", ACTOR_TYPE, nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_audit_log_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        "ix_audit_log_org_id_created_at",
        "audit_log",
        ["org_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_table(
        "usage_events",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", USAGE_KIND, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("tokens_in", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_out", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "tokens_cache_read", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "tokens_cache_write", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "units", sa.Numeric(precision=14, scale=4), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("price_version", sa.String(length=32), nullable=False),
        sa.Column("batch", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ref_type", sa.String(length=32), nullable=True),
        sa.Column("ref_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_usage_events_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_usage_events_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
    )
    op.create_index(
        "ix_usage_events_org_id_created_at", "usage_events", ["org_id", "created_at"], unique=False
    )

    for statement in [
        *audit_log_rls_sql("audit_log"),
        *append_only_sql("audit_log"),
        *enable_org_rls_sql("usage_events"),
        *append_only_sql("usage_events"),
    ]:
        op.execute(statement)


def downgrade() -> None:
    for table in ("usage_events", "audit_log"):
        for statement in disable_org_rls_sql(table):
            op.execute(statement)
    op.drop_index("ix_usage_events_org_id_created_at", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("ix_audit_log_org_id_created_at", table_name="audit_log")
    op.drop_table("audit_log")
    USAGE_KIND.drop(op.get_bind(), checkfirst=True)
    ACTOR_TYPE.drop(op.get_bind(), checkfirst=True)
