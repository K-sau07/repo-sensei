"""Shared pytest fixtures (loaded as a plugin from the repository's root conftest).

Integration fixtures talk to the real local stack started by ``make up`` and use the test
database (``SENSEI_DATABASE_TEST_NAME``). Each test runs inside a transaction that is rolled back
afterwards, so tests never see each other's data.

* ``db_session`` connects as the application role: row-level security is enforced, exactly as in
  production.
* ``owner_session`` connects as the schema owner, for tests that need DDL or cross-org setup.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sensei_core.config import Settings


def _find_alembic_ini() -> Path:
    """Locate ``packages/core/alembic.ini`` by walking up from this file."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "packages" / "core" / "alembic.ini"
        if candidate.is_file():
            return candidate
    msg = "could not find packages/core/alembic.ini above the sensei_testing package"
    raise RuntimeError(msg)


ALEMBIC_INI = _find_alembic_ini()
ROOT = ALEMBIC_INI.parents[2]


def settings_for_tests() -> Settings:
    """Settings with every database URL pointing at the test database."""
    settings = Settings(env="test")
    return settings.model_copy(
        update={"database": settings.database.for_database(settings.database.test_name)}
    )


def run_alembic(*args: str, url: str) -> None:
    """Run alembic with the current interpreter; on failure, surface alembic's own output."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "-x", f"url={url}", *args],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        raise RuntimeError(msg)


@pytest.fixture(scope="session")
def migrated_test_db() -> Settings:
    """Bring the test database to the latest schema once per test session."""
    settings = settings_for_tests()
    run_alembic("upgrade", "head", url=settings.database.migrate_url.get_secret_value())
    return settings


@asynccontextmanager
async def rolled_back_session(url: str) -> AsyncGenerator[AsyncSession]:
    """A session inside an outer transaction that is always rolled back on exit."""
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
    async with rolled_back_session(migrated_test_db.database.url.get_secret_value()) as session:
        yield session


@pytest.fixture
async def owner_session(migrated_test_db: Settings) -> AsyncGenerator[AsyncSession]:
    """Schema-owner session for DDL and cross-org fixtures; rolled back like ``db_session``."""
    url = migrated_test_db.database.migrate_url.get_secret_value()
    async with rolled_back_session(url) as session:
        yield session


@pytest.fixture
async def fake_redis() -> AsyncGenerator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()
