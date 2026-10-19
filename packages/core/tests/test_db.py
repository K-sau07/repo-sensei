from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import Settings
from sensei_core.db.base import new_id
from sensei_testing import rolled_back_session, run_alembic, settings_for_tests


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
async def test_rolled_back_session_leaves_nothing_behind(migrated_test_db: Settings) -> None:
    url = migrated_test_db.database.migrate_url.get_secret_value()
    async with rolled_back_session(url) as session:
        await session.execute(text("CREATE TABLE _isolation_probe (id int)"))
        await session.execute(text("INSERT INTO _isolation_probe VALUES (1)"))
        await session.commit()  # even an explicit commit must not escape the outer transaction

    async with rolled_back_session(url) as fresh:
        exists = await fresh.scalar(text("SELECT to_regclass('_isolation_probe') IS NOT NULL"))
    assert exists is False


@pytest.mark.integration
async def test_ids_are_generated_by_postgres_too(owner_session: AsyncSession) -> None:
    await owner_session.execute(
        text("CREATE TABLE _id_probe (id uuid PRIMARY KEY DEFAULT uuidv7(), name text)")
    )
    generated = await owner_session.scalar(
        text("INSERT INTO _id_probe (name) VALUES ('x') RETURNING id")
    )
    assert isinstance(generated, uuid.UUID)
    assert generated.version == 7


def test_constraint_names_include_every_column() -> None:
    from sqlalchemy import Column, Integer, MetaData, String, Table, UniqueConstraint  # noqa: PLC0415

    from sensei_core.db.base import NAMING_CONVENTION  # noqa: PLC0415

    table = Table(
        "repos",
        MetaData(naming_convention=NAMING_CONVENTION),
        Column("org_id", Integer),
        Column("slug", String),
        Column("name", String),
        UniqueConstraint("org_id", "slug"),
        UniqueConstraint("org_id", "name"),
    )
    names = {c.name for c in table.constraints if isinstance(c, UniqueConstraint)}
    assert names == {"uq_repos_org_id_slug", "uq_repos_org_id_name"}


@pytest.mark.integration
@pytest.mark.slow
def test_migration_roundtrip(migrated_test_db: Settings) -> None:
    url = migrated_test_db.database.migrate_url.get_secret_value()
    run_alembic("downgrade", "base", url=url)
    run_alembic("upgrade", "head", url=url)
    assert url == settings_for_tests().database.migrate_url.get_secret_value()
