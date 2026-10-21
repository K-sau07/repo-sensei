"""Recreate the throwaway database the real-API browser tests use, at the latest schema.

uv run python scripts/e2e/prepare_db.py   # needs `make up`
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from sensei_core.config import get_settings, with_database
from sensei_testing import run_alembic

E2E_DATABASE = "sensei_e2e"


async def recreate() -> None:
    settings = get_settings()
    engine = create_async_engine(
        settings.database.migrate_url.get_secret_value(), isolation_level="AUTOCOMMIT"
    )
    async with engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{E2E_DATABASE}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{E2E_DATABASE}"'))
    await engine.dispose()
    run_alembic(
        "upgrade",
        "head",
        url=with_database(settings.database.migrate_url.get_secret_value(), E2E_DATABASE),
    )


if __name__ == "__main__":
    asyncio.run(recreate())
    print(f"{E2E_DATABASE}: ready")
