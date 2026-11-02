# pyright: basic
"""Fix runs (8.5): the commit being fixed, pinned when the investigation begins.

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-02 15:11:04.693984+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fix_runs", sa.Column("commit_sha", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("fix_runs", "commit_sha")
