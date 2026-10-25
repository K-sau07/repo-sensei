"""Fresh planner and BM25 statistics after a bulk load.

Right after a stage writes a snapshot's nodes or chunks, and until autovacuum gets to them,
Postgres plans lookups without statistics for the new rows: a prefix lookup on langflow's nodes
took 61 ms instead of 1.4 ms. The stage ANALYZEs what it wrote, through the system role (which
may: migration 0023 grants it MAINTAIN on those tables).

Chunks are VACUUMed too (:func:`vacuum_chunks`): pg_textsearch counts deleted rows in a BM25
index's corpus statistics until a VACUUM, so after chunks are rewritten or pruned every score in
the partition drifts (half its rows deleted: each term's weight off by about half) until
autovacuum runs. Only the repository's partition is vacuumed, outside a transaction (VACUUM
can't run in one).
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import text

from sensei_core.logging import get_logger
from sensei_core.models.chunks import CHUNK_PARTITIONS, chunk_partitions
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)

_NAME = re.compile(r"[a-z_][a-z0-9_]*")


async def analyze(*tables: str) -> None:
    """ANALYZE ``tables``. A failure is logged, never raised: statistics only make lookups
    faster, and autovacuum gathers them anyway."""
    if not all(_NAME.fullmatch(table) for table in tables):
        msg = f"not table names: {tables!r}"
        raise ValueError(msg)
    try:
        async with get_runtime().system_database.session() as db:
            await db.execute(text(f"ANALYZE {', '.join(tables)}"))
    except Exception:
        log.warning("analyze failed", tables=list(tables), exc_info=True)


async def vacuum_chunks(repo_id: uuid.UUID) -> str | None:
    """VACUUM (ANALYZE) the partition of ``chunks`` that holds the repository's; returns its
    name. A failure is logged, never raised (autovacuum gets there in the end)."""
    partition: str | None = None
    try:
        engine = get_runtime().system_database.engine
        async with engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            remainder = await autocommit.scalar(
                text(
                    "SELECT r FROM generate_series(0, :modulus - 1) r "
                    "WHERE satisfies_hash_partition("
                    "'chunks'::regclass, :modulus, r, CAST(:repo_id AS uuid))"
                ),
                {"modulus": CHUNK_PARTITIONS, "repo_id": repo_id},
            )
            partition = chunk_partitions()[int(remainder)]
            await autocommit.execute(text(f"VACUUM (ANALYZE) {partition}"))
    except Exception:
        log.warning("vacuum failed", repo_id=str(repo_id), exc_info=True)
        return None
    return partition
