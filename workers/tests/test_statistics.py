"""Statistics after bulk loads: the repository's chunks partition vacuumed and analyzed by the
system role, outside a transaction."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from sensei_core.config import Settings
from sensei_core.db import Database
from sensei_core.models.chunks import chunk_partitions
from sensei_workers.runtime import Runtime, set_runtime
from sensei_workers.statistics import vacuum_chunks

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime(migrated_test_db: Settings) -> Iterator[Runtime]:
    current = Runtime(migrated_test_db)
    set_runtime(current)
    yield current
    set_runtime(None)


async def _counts(database: Database) -> dict[str, tuple[int, int]]:
    """Each partition's (vacuums, analyzes) so far."""
    async with database.engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT relname, vacuum_count, analyze_count FROM pg_stat_all_tables "
                "WHERE relname LIKE 'chunks_p%'"
            )
        )
        return {str(row.relname): (int(row.vacuum_count), int(row.analyze_count)) for row in rows}


def test_the_repositorys_partition_alone_is_vacuumed_and_analyzed(runtime: Runtime) -> None:
    database = runtime.system_database
    repo_id = uuid.uuid4()
    before = runtime.run(_counts(database))
    partition = runtime.run(vacuum_chunks(repo_id))
    assert partition in chunk_partitions()

    async def settled() -> dict[str, tuple[int, int]]:
        for _ in range(50):  # the cumulative statistics are flushed asynchronously
            after = await _counts(database)
            if after[partition] != before[partition]:
                return after
            await asyncio.sleep(0.1)
        return await _counts(database)

    after = runtime.run(settled())
    vacuums, analyzes = after[partition]
    assert (vacuums, analyzes) > before[partition]
    assert vacuums == before[partition][0] + 1
    assert analyzes == before[partition][1] + 1
    others = {name: counts for name, counts in after.items() if name != partition}
    assert others == {name: before[name] for name in others}
