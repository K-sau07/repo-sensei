# pyright: basic
"""repositories published to explore

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-04 09:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # When the explore org's admins published it to the public gallery (10.2); null: not shown.
    op.add_column("repos", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("repos", "published_at")
