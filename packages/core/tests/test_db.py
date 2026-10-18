from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.base import new_id
from sensei_core.testing import run_alembic, settings_for_tests


def test_new_ids_are_uuid7_and_time_ordered() -> None:
    ids = [new_id() for _ in range(50)]
    assert all(isinstance(i, uuid.UUID) and i.version == 7 for i in ids)
    assert ids == sorted(ids)


@pytest.mark.integration
async def test_extensions_present(db_session: AsyncSession) -> None:
    rows = await db_session.execute(text("SELECT extname FROM pg_extension"))
    names = {row[0] for row in rows}
    assert {"vector", "pg_trgm", "pg_textsearch", "citext"} <= names


@pytest.mark.integration
async def test_changes_are_rolled_back_part_1(db_session: AsyncSession) -> None:
    await db_session.execute(text("CREATE TABLE _isolation_probe (id int)"))
    await db_session.execute(text("INSERT INTO _isolation_probe VALUES (1)"))
    await db_session.commit()  # even an explicit commit must not leak out of the test


@pytest.mark.integration
async def test_changes_are_rolled_back_part_2(db_session: AsyncSession) -> None:
    exists = await db_session.scalar(text("SELECT to_regclass('_isolation_probe') IS NOT NULL"))
    assert exists is False


@pytest.mark.integration
@pytest.mark.slow
def test_migration_roundtrip(migrated_test_db: str) -> None:
    url = settings_for_tests().database.url.get_secret_value()
    run_alembic("downgrade", "base", url=url)
    run_alembic("upgrade", "head", url=url)
    assert migrated_test_db == url
