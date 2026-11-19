# pyright: basic
"""The free plan's trial: how many repositories each person has connected to orgs on it

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-19 21:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("trial_repos", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("users", "trial_repos")
