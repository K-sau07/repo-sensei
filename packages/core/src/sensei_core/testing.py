"""Shared pytest fixtures (loaded as a plugin from the repository's root conftest).

Integration fixtures talk to the real local stack started by ``make up`` and use the test
database (``SENSEI_DATABASE_TEST_NAME``). Each test runs inside a transaction that is rolled back
afterwards, so tests never see each other's data.

* ``db_session`` connects as the application role: row-level security is enforced, exactly as in
  production.
* ``owner_session`` connects as the schema owner, for tests that need DDL or cross-org setup.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sensei_core.config import Settings

ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = ROOT / "packages" / "core" / "alembic.ini"


def settings_for_tests() -> Settings:
    """Settings with every database URL pointing at the test database."""
    settings = Settings(env="test")
    return settings.model_copy(
        update={"database": settings.database.for_database(settings.database.test_name)}
    )


def run_alembic(*args: str, url: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["uv", "run", "alembic", "-c", str(ALEMBIC_INI), "-x", f"url={url}", *args],  # noqa: S607
        check=True,
        cwd=ROOT,
        capture_output=True,
        env={**os.environ},
    )


@pytest.fixture(scope="session")
def migrated_test_db() -> Settings:
    """Bring the test database to the latest schema once per test session."""
    settings = settings_for_tests()
    run_alembic("upgrade", "head", url=settings.database.migrate_url.get_secret_value())
    return settings


async def _rolled_back_session(url: str) -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        outer = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as session:
            yield session
        await outer.rollback()
    await engine.dispose()


@pytest.fixture
async def db_session(migrated_test_db: Settings) -> AsyncGenerator[AsyncSession]:
    """App-role session (RLS enforced); everything is rolled back, even explicit commits."""
    async for session in _rolled_back_session(migrated_test_db.database.url.get_secret_value()):
        yield session


@pytest.fixture
async def owner_session(migrated_test_db: Settings) -> AsyncGenerator[AsyncSession]:
    """Schema-owner session for DDL and cross-org fixtures; rolled back like ``db_session``."""
    url = migrated_test_db.database.migrate_url.get_secret_value()
    async for session in _rolled_back_session(url):
        yield session


@pytest.fixture
async def fake_redis() -> AsyncGenerator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()
