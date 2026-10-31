# pyright: basic
"""What an index job indexes (5.1): ``ref``, the branch it is of, and ``target_sha``, the commit a
push named; both null for a run of the default branch's head (a connect, or Index again).

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-31 06:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("index_jobs", sa.Column("ref", sa.String(length=255), nullable=True))
    op.add_column("index_jobs", sa.Column("target_sha", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("index_jobs", "target_sha")
    op.drop_column("index_jobs", "ref")
