# pyright: basic
"""a diagram of the whole app (its modules, by tier)

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-11 10:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE diagram_source ADD VALUE IF NOT EXISTS 'repo'")


def downgrade() -> None:
    # Postgres can't drop an enum's value: the diagrams of the whole app go, the value stays
    # (unused, harmless).
    op.execute("DELETE FROM diagrams WHERE source = 'repo'")
