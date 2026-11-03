# pyright: basic
"""conversations asked by no one signed in

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-04 10:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A question asked on a public gallery page (10.2): no one's, listed for no one.
    op.alter_column("conversations", "user_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    # Row-level security off, so a role it would filter fails here instead of deleting nothing.
    op.execute("SET LOCAL row_security = off")
    op.execute("DELETE FROM conversations WHERE user_id IS NULL")
    op.alter_column("conversations", "user_id", existing_type=sa.Uuid(), nullable=False)
