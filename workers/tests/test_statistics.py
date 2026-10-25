"""Statistics after bulk loads: the repository's chunks partition analyzed, or vacuumed and
analyzed outside a transaction, by the system role; vacuums asked for are folded per partition."""

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
from sensei_workers.statistics import analyze_chunks, chunk_partition_of, vacuum_partition
from sensei_workers.tasks import maintenance

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


async def _settled(
    database: Database, before: dict[str, tuple[int, int]], name: str
) -> dict[str, tuple[int, int]]:
    for _ in range(50):  # the cumulative statistics are flushed asynchronously
        after = await _counts(database)
        if after[name] != before[name]:
            return after
        await asyncio.sleep(0.1)
    return await _counts(database)


def _changed(
    runtime: Runtime, before: dict[str, tuple[int, int]], partition: str
) -> tuple[int, int]:
    """How many more vacuums and analyzes the partition had (the others had none)."""
    name = partition.removeprefix("public.")
    after = runtime.run(_settled(runtime.system_database, before, name))
    others = {other: counts for other, counts in after.items() if other != name}
    assert others == {other: before[other] for other in others}
    return after[name][0] - before[name][0], after[name][1] - before[name][1]


def test_the_repositorys_partition_alone_is_vacuumed_and_analyzed(runtime: Runtime) -> None:
    repo_id = uuid.uuid4()
    partition = runtime.run(chunk_partition_of(repo_id))
    assert partition.removeprefix("public.") in chunk_partitions()
    before = runtime.run(_counts(runtime.system_database))
    runtime.run(vacuum_partition(partition))
    assert _changed(runtime, before, partition) == (1, 1)


def test_rows_written_alone_get_an_analyze(runtime: Runtime) -> None:
    repo_id = uuid.uuid4()
    partition = runtime.run(chunk_partition_of(repo_id))
    before = runtime.run(_counts(runtime.system_database))
    runtime.run(analyze_chunks(repo_id))
    assert _changed(runtime, before, partition) == (0, 1)


def test_only_partitions_of_chunks_are_vacuumed(runtime: Runtime) -> None:
    with pytest.raises(ValueError, match="not a partition"):
        runtime.run(vacuum_partition("public.snapshots"))
    with pytest.raises(ValueError, match="not a partition"):
        runtime.run(vacuum_partition("chunks_p00"))  # unqualified: whatever the path finds


def test_vacuums_asked_for_meanwhile_are_one(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[dict[str, str], int]] = []

    def apply_async(*, kwargs: dict[str, str], countdown: int) -> None:
        sent.append((kwargs, countdown))

    monkeypatch.setattr(maintenance.vacuum_chunks, "apply_async", apply_async)
    repo_id = uuid.uuid4()
    partition = runtime.run(chunk_partition_of(repo_id))
    runtime.sync_redis.delete(maintenance.vacuum_key(partition))
    try:
        assert maintenance.request_vacuum(repo_id)
        assert not maintenance.request_vacuum(repo_id)  # one is waiting
        assert sent == [({"partition": partition}, maintenance.DEBOUNCE_SECONDS)]
        # The vacuum, as it starts, lets the next request through.
        before = runtime.run(_counts(runtime.system_database))
        assert maintenance.vacuum_chunks.apply(kwargs={"partition": partition}).get() == partition
        assert _changed(runtime, before, partition) == (1, 1)
        assert maintenance.request_vacuum(repo_id)
        assert len(sent) == 2
    finally:
        runtime.sync_redis.delete(maintenance.vacuum_key(partition))
