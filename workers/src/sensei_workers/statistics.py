"""Fresh planner statistics after a bulk load.

Right after a stage writes a snapshot's nodes or chunks, and until autovacuum gets to them,
Postgres plans lookups without statistics for the new rows: a prefix lookup on langflow's nodes
took 61 ms instead of 1.4 ms. The stage ANALYZEs what it wrote, through the system role (which
may: migration 0023 grants it MAINTAIN on those tables).
"""

from __future__ import annotations

import re

from sqlalchemy import text

from sensei_core.logging import get_logger
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
