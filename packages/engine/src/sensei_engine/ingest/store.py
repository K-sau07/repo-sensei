"""Saving a discovery: the snapshot's file rows, its summary and the repository's languages.

Rows go in with ``COPY`` (asyncpg's binary copy), tens of thousands of rows a second. Postgres
doesn't allow ``COPY FROM`` into a table with row-level security, so they are copied into a
temporary table first and moved over with ``INSERT … SELECT``, which the ``files`` policy still
checks: the rows can only land in the session's org.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import Repo, RepoFile, Snapshot
from sensei_engine.ingest.discover import Discovery

COLUMNS = (
    "org_id",
    "repo_id",
    "snapshot_id",
    "path",
    "blob_sha",
    "content_hash",
    "language",
    "size",
    "loc",
    "role",
    "flags",
    "framework_root",
)
STAGING_TABLE = "files_staging"


async def save_discovery(db: AsyncSession, snapshot: Snapshot, discovery: Discovery) -> int:
    """Replace the snapshot's files with ``discovery``'s; returns the rows written. Runs in the
    caller's transaction, which must have the snapshot's org set."""
    await db.execute(delete(RepoFile).where(RepoFile.snapshot_id == snapshot.id))
    records = [
        (
            snapshot.org_id,
            snapshot.repo_id,
            snapshot.id,
            file.path,
            file.blob_sha,
            file.content_hash,
            file.language,
            file.size,
            file.loc,
            file.role.value,
            list(file.flags),
            file.framework_root,
        )
        for file in discovery.files
    ]
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    driver = cast("Any", raw.driver_connection)  # asyncpg.Connection
    columns = ", ".join(COLUMNS)
    # Dropped at the end of the transaction, or right away when all went well (so this can run
    # again in the same transaction). On an error the transaction rolls back and takes it along.
    await driver.execute(
        f"CREATE TEMPORARY TABLE {STAGING_TABLE} (LIKE files INCLUDING DEFAULTS) ON COMMIT DROP"
    )
    await driver.copy_records_to_table(STAGING_TABLE, records=records, columns=list(COLUMNS))
    await driver.execute(
        f"INSERT INTO files (id, {columns}) SELECT id, {columns} FROM {STAGING_TABLE}"  # noqa: S608 - constant names
    )
    await driver.execute(f"DROP TABLE {STAGING_TABLE}")

    snapshot.file_count = len(discovery.files)
    snapshot.loc = discovery.loc
    snapshot.bytes = discovery.bytes
    snapshot.summary = discovery.summary()
    repo = await db.get(Repo, snapshot.repo_id)
    if repo is not None:
        repo.languages = discovery.github_languages()
    await db.flush()
    return len(records)
