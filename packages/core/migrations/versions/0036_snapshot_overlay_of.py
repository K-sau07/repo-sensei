# pyright: basic
"""Branch and pull request overlays (5.4): ``snapshots.overlay_of``, the snapshot a branch's (or a
pull request's) was built on (its unchanged files' facts carried from it); null for the default
branch's, and for a branch's built in full.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-31 08:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("snapshots", sa.Column("overlay_of", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_snapshots_overlay_of",
        "snapshots",
        "snapshots",
        ["overlay_of"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_snapshots_repo_id_ref", "snapshots", ["repo_id", "ref"])


def downgrade() -> None:
    op.drop_index("ix_snapshots_repo_id_ref", table_name="snapshots")
    op.drop_constraint("fk_snapshots_overlay_of", "snapshots", type_="foreignkey")
    op.drop_column("snapshots", "overlay_of")
