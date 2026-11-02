# pyright: basic
"""Fix runs (8.4): what a run has spent against the fixer's budget (its agents' turns, model
cost, sandbox time) and why it stopped short.

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-02 09:04:19.324569+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fix_runs", sa.Column("stop_reason", sa.Text(), nullable=True))
    op.add_column(
        "fix_runs", sa.Column("spent_turns", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "fix_runs",
        sa.Column(
            "spent_cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "fix_runs",
        sa.Column("spent_sandbox_seconds", sa.Float(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("fix_runs", "spent_sandbox_seconds")
    op.drop_column("fix_runs", "spent_cost_usd")
    op.drop_column("fix_runs", "spent_turns")
    op.drop_column("fix_runs", "stop_reason")
