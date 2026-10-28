# pyright: basic
"""An index on ``edges.evidence_file_id``: its foreign key cascades from ``files``, so deleting a
file (pruning a snapshot, a repository) looks its edges up by it, and without one scanned every
edge per file.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-27 20:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_edges_evidence_file_id", "edges", ["evidence_file_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_edges_evidence_file_id", table_name="edges")
