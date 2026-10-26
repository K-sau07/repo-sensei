# pyright: basic
"""A chunk's generation has no default: every writer names the run that wrote the row. With the
default (0, from 0024's backfill) a writer that left it out would write rows search never reads
and the next prune deletes, without an error. ``ALTER COLUMN`` reaches every partition.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-26 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("chunks", "generation", server_default=None)


def downgrade() -> None:
    op.alter_column("chunks", "generation", server_default="0")
