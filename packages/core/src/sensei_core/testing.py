"""Shared pytest fixtures (loaded as a plugin from the repository's root conftest).

Integration fixtures talk to the real local stack started by ``make up`` and use the
``sensei_test`` database. Each test runs inside a transaction that is rolled back afterwards,
so tests never see each other's data.
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
    """Settings pointing every database URL at the test database."""
    settings = Settings(env="test")
    test_url = settings.database.url_test
    return settings.model_copy(
        update={"database": settings.database.model_copy(update={"url": test_url})}
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
def migrated_test_db() -> str:
    """Bring the test database to the latest schema once per test session."""
    url = settings_for_tests().database.url.get_secret_value()
    run_alembic("upgrade", "head", url=url)
    return url


@pytest.fixture
async def db_session(migrated_test_db: str) -> AsyncGenerator[AsyncSession]:
    """A session whose work is always rolled back, even if the code under test commits."""
    engine = create_async_engine(migrated_test_db)
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
async def fake_redis() -> AsyncGenerator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()
