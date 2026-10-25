# pyright: basic
"""Chunk generations: each run of the chunks stage writes a snapshot's chunks under a new
generation (from ``chunk_generations``, so two runs never share one), committed in batches; the
snapshot's live generation (``snapshots.chunk_generation``) is what search reads, and changes in
one short transaction when a run finishes. Chunks already written are generation 0, live for the
snapshots that were chunked.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-25 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The roles get USAGE on new sequences by default privileges (0002).
    op.execute("CREATE SEQUENCE chunk_generations AS bigint START WITH 1")
    # A constant default: no rewrite of the partitions.
    op.add_column(
        "chunks", sa.Column("generation", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column("snapshots", sa.Column("chunk_generation", sa.BigInteger(), nullable=True))
    op.execute("UPDATE snapshots SET chunk_generation = 0 WHERE chunk_count IS NOT NULL")


def downgrade() -> None:
    op.drop_column("snapshots", "chunk_generation")
    op.drop_column("chunks", "generation")
    op.execute("DROP SEQUENCE chunk_generations")
