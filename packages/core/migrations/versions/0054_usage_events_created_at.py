# pyright: basic
"""usage events by time

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-03 19:10:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every org's spend since midnight, summed each minute for the global cap (10.1).
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_usage_events_created_at", table_name="usage_events")
