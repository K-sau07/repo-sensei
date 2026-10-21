# pyright: basic
"""files.fast_hash: the xxh3-128 of each file (the Merkle tree stores it too).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20 17:23:59.132522+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("files", sa.Column("fast_hash", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("files", "fast_hash")
