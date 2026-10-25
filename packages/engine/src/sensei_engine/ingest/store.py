"""Saving a discovery: the snapshot's file rows, its summary and the repository's languages.

Rows go in with ``COPY`` (asyncpg's binary copy), tens of thousands of rows a second. Postgres
doesn't allow ``COPY FROM`` into a table with row-level security, so they are copied into a
temporary table first and moved over with ``INSERT … SELECT``, which the ``files`` policy still
checks: the rows can only land in the session's org. Row ids come from discovery (see
:class:`~sensei_engine.ingest.discover.DiscoveredFile`).

Saving a snapshot's files again (a reindex of the same commit) updates them in place: a path the
snapshot already had keeps its row and id (discover with :func:`file_ids` so the discovery's ids
are those too), so what hangs off it (its symbols and chunks) stays, and search keeps serving the
snapshot while the later stages rebuild. Only the files that are gone are deleted, their symbols
and chunks with them (on delete cascade).
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import Repo, RepoFile, Snapshot
from sensei_engine.ingest.discover import Discovery

COLUMNS = (
    "id",
    "org_id",
    "repo_id",
    "snapshot_id",
    "path",
    "blob_sha",
    "content_hash",
    "fast_hash",
    "language",
    "size",
    "loc",
    "role",
    "flags",
    "framework_root",
)
# What a rediscovery may change about a path the snapshot has (not its id, owner or path).
UPDATED = tuple(c for c in COLUMNS if c not in ("id", "org_id", "repo_id", "snapshot_id", "path"))
STAGING_TABLE = "files_staging"


async def file_ids(db: AsyncSession, snapshot_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """The ids of the snapshot's files, by path."""
    rows = await db.execute(
        select(RepoFile.path, RepoFile.id).where(RepoFile.snapshot_id == snapshot_id)
    )
    return dict(rows.tuples().all())


async def save_discovery(db: AsyncSession, snapshot: Snapshot, discovery: Discovery) -> int:
    """Make the snapshot's files ``discovery``'s; returns the rows written. Paths it already
    had keep their rows (see the module); the others are deleted. Runs in the caller's
    transaction, which must have the snapshot's org set."""
    records = [
        (
            file.id,
            snapshot.org_id,
            snapshot.repo_id,
            snapshot.id,
            file.path,
            file.blob_sha,
            file.content_hash,
            file.fast_hash,
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
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in UPDATED)
    await driver.execute(
        f"INSERT INTO files ({columns}) SELECT {columns} FROM {STAGING_TABLE} "  # noqa: S608 - constant names
        f"ON CONFLICT (snapshot_id, path) DO UPDATE SET {updates}"
    )
    await driver.execute(
        f"DELETE FROM files f WHERE f.snapshot_id = $1 AND NOT EXISTS "  # noqa: S608 - constant names
        f"(SELECT FROM {STAGING_TABLE} s WHERE s.path = f.path)",
        snapshot.id,
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
