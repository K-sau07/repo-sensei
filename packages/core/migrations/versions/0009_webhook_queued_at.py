# pyright: basic
"""Webhook deliveries remember when they were last queued (the sweep re-queues at most every
few minutes, not on every run).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 21:05:18.212641+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "webhook_deliveries",
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_waiting",
        "webhook_deliveries",
        ["queued_at"],
        postgresql_where=sa.text("status = 'received'"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_waiting", table_name="webhook_deliveries")
    op.drop_column("webhook_deliveries", "queued_at")
