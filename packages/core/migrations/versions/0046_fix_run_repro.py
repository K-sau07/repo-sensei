# pyright: basic
"""Fix runs (8.6): the reproducing test and its runs.

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-02 15:24:08.327415+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fix_runs", sa.Column("repro", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("fix_runs", "repro")
