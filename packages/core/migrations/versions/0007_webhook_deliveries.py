# pyright: basic
"""Webhook deliveries: a system inbox (no org scope); the app role may only insert.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19 20:07:30.290611+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import insert_only_sql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELIVERY_STATUS = postgresql.ENUM(
    "received",
    "processed",
    "stored",
    "ignored",
    "failed",
    name="delivery_status",
    create_type=False,
)


def upgrade() -> None:
    DELIVERY_STATUS.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "webhook_deliveries",
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.Column("installation_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", DELIVERY_STATUS, server_default="received", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
        sa.UniqueConstraint("delivery_id", name=op.f("uq_webhook_deliveries_delivery_id")),
    )
    op.create_index(
        op.f("ix_webhook_deliveries_installation_id"),
        "webhook_deliveries",
        ["installation_id"],
        unique=False,
    )
    for statement in insert_only_sql("webhook_deliveries"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(op.f("ix_webhook_deliveries_installation_id"), table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    DELIVERY_STATUS.drop(op.get_bind(), checkfirst=True)
