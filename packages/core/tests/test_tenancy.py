"""Prove org isolation with real roles and a real RLS policy.

A probe table is created (committed) by the schema owner, rows for two orgs are inserted, and
then every assertion runs over a separate connection as ``sensei_app`` (RLS enforced) or
``sensei_system`` (BYPASSRLS). The probe table is dropped afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from sensei_core.config import Settings
from sensei_core.db.tenancy import (
    current_org,
    disable_org_rls_sql,
    enable_org_rls_sql,
    set_org_context,
    set_share_context,
    shared_rls_sql,
)

pytestmark = pytest.mark.integration

ORG_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
ORG_B = uuid.UUID("00000000-0000-7000-8000-00000000000b")


@pytest.fixture
async def probe(migrated_test_db: Settings) -> AsyncGenerator[str]:
    table = f"_rls_probe_{uuid.uuid4().hex[:8]}"
    owner = create_async_engine(migrated_test_db.database.migrate_url.get_secret_value())
    async with owner.begin() as conn:
        await conn.execute(text(f"CREATE TABLE {table} (id int PRIMARY KEY, org_id uuid NOT NULL)"))
        for statement in enable_org_rls_sql(table):
            await conn.execute(text(statement))
        await conn.execute(
            text(f"INSERT INTO {table} VALUES (1, :a), (2, :a), (3, :b)"), {"a": ORG_A, "b": ORG_B}
        )
    try:
        yield table
    finally:
        async with owner.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        await owner.dispose()


@pytest.fixture
async def app_engine(migrated_test_db: Settings) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(migrated_test_db.database.url.get_secret_value())
    yield engine
    await engine.dispose()


@pytest.fixture
async def system_engine(migrated_test_db: Settings) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(migrated_test_db.database.system_url.get_secret_value())
    yield engine
    await engine.dispose()


async def _ids(session: AsyncSession, table: str) -> list[int]:
    return list((await session.scalars(text(f"SELECT id FROM {table} ORDER BY id"))).all())


async def test_app_role_cannot_bypass_rls(app_engine: AsyncEngine) -> None:
    async with AsyncSession(app_engine) as session:
        row = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert tuple(row) == (False, False)


async def test_org_context_limits_reads(probe: str, app_engine: AsyncEngine) -> None:
    async with AsyncSession(app_engine) as session, session.begin():
        await set_org_context(session, ORG_A)
        assert await current_org(session) == ORG_A
        assert await _ids(session, probe) == [1, 2]
    async with AsyncSession(app_engine) as session, session.begin():
        await set_org_context(session, ORG_B)
        assert await _ids(session, probe) == [3]


async def test_no_org_context_sees_nothing(probe: str, app_engine: AsyncEngine) -> None:
    async with AsyncSession(app_engine) as session, session.begin():
        assert await current_org(session) is None
        assert await _ids(session, probe) == []


async def test_context_is_transaction_local(probe: str, app_engine: AsyncEngine) -> None:
    async with AsyncSession(app_engine) as session:
        async with session.begin():
            await set_org_context(session, ORG_A)
            assert await _ids(session, probe) == [1, 2]
        async with session.begin():  # new transaction on the same connection: context is gone
            assert await _ids(session, probe) == []


async def test_cannot_update_or_delete_other_org(probe: str, app_engine: AsyncEngine) -> None:
    async with AsyncSession(app_engine) as session, session.begin():
        await set_org_context(session, ORG_A)
        updated = cast(
            "CursorResult[Any]",
            await session.execute(text(f"UPDATE {probe} SET id = 30 WHERE id = 3")),
        )
        deleted = cast(
            "CursorResult[Any]", await session.execute(text(f"DELETE FROM {probe} WHERE id = 3"))
        )
        assert updated.rowcount == 0
        assert deleted.rowcount == 0


async def _execute_as_org(
    engine: AsyncEngine, org: uuid.UUID, sql: str, params: dict[str, object]
) -> None:
    async with AsyncSession(engine) as session, session.begin():
        await set_org_context(session, org)
        await session.execute(text(sql), params)


async def test_cannot_insert_into_other_org(probe: str, app_engine: AsyncEngine) -> None:
    insert = f"INSERT INTO {probe} VALUES (9, :b)"
    with pytest.raises(DBAPIError, match="row-level security"):
        await _execute_as_org(app_engine, ORG_A, insert, {"b": ORG_B})


async def test_cannot_move_row_to_other_org(probe: str, app_engine: AsyncEngine) -> None:
    move = f"UPDATE {probe} SET org_id = :b WHERE id = 1"
    with pytest.raises(DBAPIError, match="row-level security"):
        await _execute_as_org(app_engine, ORG_A, move, {"b": ORG_B})


@pytest.fixture
async def shared_probe(migrated_test_db: Settings) -> AsyncGenerator[str]:
    """Conversations as the share policy sees them: two shared (in two orgs), one not."""
    table = f"_share_probe_{uuid.uuid4().hex[:8]}"
    owner = create_async_engine(migrated_test_db.database.migrate_url.get_secret_value())
    async with owner.begin() as conn:
        await conn.execute(
            text(
                f"CREATE TABLE {table} (id int PRIMARY KEY, org_id uuid NOT NULL, title text,"
                " share_token_hash varchar(64) UNIQUE)"
            )
        )
        for statement in [*enable_org_rls_sql(table), *shared_rls_sql(table)]:
            await conn.execute(text(statement))
        await conn.execute(
            text(
                f"INSERT INTO {table} VALUES"
                " (1, :a, '', 'aa'), (2, :b, '', 'bb'), (3, :a, '', NULL)"
            ),
            {"a": ORG_A, "b": ORG_B},
        )
    try:
        yield table
    finally:
        async with owner.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        await owner.dispose()


async def test_a_share_token_reads_its_one_row_and_writes_nothing(
    shared_probe: str, app_engine: AsyncEngine
) -> None:
    async with AsyncSession(app_engine) as session, session.begin():
        assert await _ids(session, shared_probe) == []
        await set_share_context(session, "bb")
        assert await _ids(session, shared_probe) == [2]  # in any org, no org context needed
        await set_share_context(session, "cc")
        assert await _ids(session, shared_probe) == []
        await set_share_context(session, "")
        assert await _ids(session, shared_probe) == []  # not the rows never shared
    async with AsyncSession(app_engine) as session, session.begin():
        await set_share_context(session, "aa")
        changed = cast(
            "CursorResult[Any]",
            await session.execute(text(f"UPDATE {shared_probe} SET title = 'x' WHERE id = 1")),
        )
        assert changed.rowcount == 0


async def test_system_role_sees_every_org(probe: str, system_engine: AsyncEngine) -> None:
    async with AsyncSession(system_engine) as session, session.begin():
        assert await _ids(session, probe) == [1, 2, 3]


def test_rls_sql_round_trip() -> None:
    enable = enable_org_rls_sql("things")
    disable = disable_org_rls_sql("things")
    assert any("FORCE ROW LEVEL SECURITY" in s for s in enable)
    assert any("CREATE POLICY org_isolation" in s for s in enable)
    assert any("DROP POLICY IF EXISTS org_isolation" in s for s in disable)
    assert any("DROP POLICY IF EXISTS share_by_token" in s for s in disable)
