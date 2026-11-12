# pyright: basic
"""ER diagrams: a schema's tables, their columns and the foreign keys between them

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-12 10:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE diagram_kind ADD VALUE IF NOT EXISTS 'er'")


def downgrade() -> None:
    # Postgres can't drop an enum's value: the ER diagrams go, the value stays (unused).
    op.execute("DELETE FROM diagrams WHERE kind = 'er'")
