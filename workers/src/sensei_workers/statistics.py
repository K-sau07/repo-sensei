"""Fresh planner and BM25 statistics after a bulk load.

Right after a stage writes a snapshot's nodes or chunks, and until autovacuum gets to them,
Postgres plans lookups without statistics for the new rows: a prefix lookup on langflow's nodes
took 61 ms instead of 1.4 ms. The stage ANALYZEs what it wrote, through the system role (which
may: migration 0023 grants it MAINTAIN on those tables).

Chunks are VACUUMed when rows of them were deleted (:func:`vacuum_partition`, run by its own
task: :mod:`sensei_workers.tasks.maintenance`): pg_textsearch counts deleted rows in a BM25
index's corpus statistics until a VACUUM, so after chunks are replaced or pruned every score in
the partition drifts (half its rows deleted: each term's weight off by about half) until
autovacuum runs. Only the repository's partition is vacuumed, outside a transaction (VACUUM
can't run in one). Every statement here waits at most :data:`LOCK_TIMEOUT` for its lock, so
maintenance never queues behind (or holds up) DDL.
"""

from __future__ import annotations

import re
import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text

from sensei_core.logging import get_logger
from sensei_core.models.chunks import CHUNK_PARTITIONS, chunk_partitions
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)

SCHEMA = "public"
LOCK_TIMEOUT = "5s"
VACUUM_TIMEOUT = "15min"
_NAME = re.compile(r"[a-z_][a-z0-9_]*")
_PARTITIONS = frozenset(f"{SCHEMA}.{name}" for name in chunk_partitions())


async def analyze(*tables: str) -> None:
    """ANALYZE ``tables``. A failure is logged, never raised: statistics only make lookups
    faster, and autovacuum gathers them anyway (a task's time limit still stops it)."""
    if not all(_NAME.fullmatch(table) for table in tables):
        msg = f"not table names: {tables!r}"
        raise ValueError(msg)
    try:
        async with get_runtime().system_database.session() as db:
            await db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            await db.execute(text(f"ANALYZE {', '.join(SCHEMA + '.' + t for t in tables)}"))
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        log.warning("analyze failed", tables=list(tables), exc_info=True)


async def chunk_partition_of(repo_id: uuid.UUID) -> str:
    """The partition of ``chunks`` that holds (or will hold) the repository's rows:
    ``public.chunks_pNN``."""
    async with get_runtime().system_database.session() as db:
        remainder = await db.scalar(
            text(
                "SELECT r FROM generate_series(0, :modulus - 1) r "  # noqa: S608 - a constant
                "WHERE satisfies_hash_partition("
                f"'{SCHEMA}.chunks'::regclass, :modulus, r, CAST(:repo_id AS uuid))"
            ),
            {"modulus": CHUNK_PARTITIONS, "repo_id": repo_id},
        )
    return f"{SCHEMA}.{chunk_partitions()[int(remainder)]}"


async def analyze_chunks(repo_id: uuid.UUID) -> None:
    """ANALYZE the repository's partition (rows written, none deleted). Logged on failure."""
    try:
        partition = await chunk_partition_of(repo_id)
        async with get_runtime().system_database.session() as db:
            await db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            await db.execute(text(f"ANALYZE {partition}"))
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        log.warning("analyze failed", repo_id=str(repo_id), exc_info=True)


async def vacuum_partition(partition: str) -> None:
    """VACUUM (ANALYZE) one partition of ``chunks``, outside a transaction; raises on failure
    (a lock not had within :data:`LOCK_TIMEOUT` included)."""
    if partition not in _PARTITIONS:
        msg = f"not a partition of chunks: {partition!r}"
        raise ValueError(msg)
    engine = get_runtime().system_database.engine
    async with engine.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        await autocommit.execute(text(f"SET statement_timeout = '{VACUUM_TIMEOUT}'"))
        try:
            await autocommit.execute(text(f"VACUUM (ANALYZE) {partition}"))
        finally:
            await autocommit.execute(text("RESET lock_timeout"))
            await autocommit.execute(text("RESET statement_timeout"))
