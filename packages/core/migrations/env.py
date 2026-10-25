"""Alembic environment (async).

Migrations always run as the schema owner: ``SENSEI_DATABASE_MIGRATE_URL`` by default, or an
explicit ``-x url=...`` (used by the test suite to migrate the test database).
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import sensei_core.models  # noqa: F401  # pyright: ignore[reportUnusedImport] - registers every model on Base.metadata
from sensei_core.config import get_settings
from sensei_core.db.base import Base
from sensei_core.models.chunks import chunk_partitions

target_metadata = Base.metadata
# Partitions are made by migrations, not models: comparing the schema leaves them out.
PARTITIONS = frozenset(chunk_partitions())


def _include_name(name: str | None, type_: str, _parents: object) -> bool:
    return not (type_ == "table" and name in PARTITIONS)


def _database_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("url")
    return override or get_settings().database.migrate_url.get_secret_value()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_online())
