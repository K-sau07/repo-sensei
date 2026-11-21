# pyright: basic
"""The free plan's other counts: each person's questions and model-drawn diagrams on it

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-21 12:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("trial_asks", "trial_diagrams"):
        op.add_column(
            "users", sa.Column(column, sa.Integer(), server_default=sa.text("0"), nullable=False)
        )


def downgrade() -> None:
    for column in ("trial_diagrams", "trial_asks"):
        op.drop_column("users", column)
