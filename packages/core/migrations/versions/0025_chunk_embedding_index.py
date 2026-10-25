# pyright: basic
"""Chunk embeddings' HNSW indexes, one per partition of ``chunks``, built after a bulk load: the
function ``chunk_embedding_index(partition)`` builds the partition's index if it has none and
says whether it did. It runs as the tables' owner (only an owner may create an index), for the
system role alone.

Building an index over rows already loaded is several times faster than inserting into one row
by row, so the embeddings stage loads a partition's first vectors without an index and calls the
function once they are in; later loads go into the index as they come. A per-partition advisory
lock keeps two stages from building the same index at once (the second finds it built).

``maintenance_work_mem`` is capped for the build: a parallel build keeps the graph in shared
memory up to that size, and must not ask for more than the server's ``/dev/shm`` holds.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-25 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from sensei_core.db.tenancy import SYSTEM_ROLE

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARTITIONS = 16


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION chunk_embedding_index(partition text) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
        BEGIN
            IF partition !~ '^chunks_p[0-9]{2}$' OR NOT EXISTS (
                SELECT 1 FROM pg_inherits
                WHERE inhrelid = to_regclass(partition) AND inhparent = 'chunks'::regclass
            ) THEN
                RAISE EXCEPTION 'not a partition of chunks: %', partition;
            END IF;
            PERFORM pg_advisory_xact_lock(hashtext('chunk_embedding_index'), hashtext(partition));
            IF to_regclass(partition || '_embedding_hnsw') IS NOT NULL THEN
                RETURN false;
            END IF;
            PERFORM set_config('maintenance_work_mem', '512MB', true);
            EXECUTE format(
                'CREATE INDEX %I ON %I USING hnsw (embedding halfvec_cosine_ops) '
                'WITH (m = 16, ef_construction = 128)',
                partition || '_embedding_hnsw', partition
            );
            RETURN true;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION chunk_embedding_index(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION chunk_embedding_index(text) TO {SYSTEM_ROLE}")


def downgrade() -> None:
    for remainder in range(PARTITIONS):
        op.execute(f"DROP INDEX IF EXISTS chunks_p{remainder:02}_embedding_hnsw")
    op.execute("DROP FUNCTION chunk_embedding_index(text)")
