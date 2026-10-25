"""Saving a snapshot's chunks as ``chunks`` rows, in batches as they're made.

Rows go in with ``COPY`` into a temporary table, :data:`BATCH_ROWS` at a time, then
``INSERT … SELECT`` into ``chunks`` (where row-level security checks every row: they can only land
in the session's org). The insert links each definition's chunks to its node by symbol key, so
the snapshot's symbols are saved first. Everything happens in the caller's transaction: a
snapshot's chunks are swapped all at once.

A full save (``replace_all``) deletes the snapshot's chunks first; an incremental one deletes
the chunks owned by each file it saves (see :func:`delete_owned_chunks`). Once a snapshot is the
repository's indexed one, the other snapshots' chunks are pruned (:func:`prune_chunks`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from sensei_core.models import CodeChunk, Snapshot
from sensei_engine.chunk.model import Chunk

BATCH_ROWS = 5000
STAGING_TABLE = "chunks_staging"
COLUMNS = (
    "id",
    "org_id",
    "repo_id",
    "snapshot_id",
    "file_id",
    "owner_file_id",
    "symbol_key",
    "view",
    "kind",
    "path",
    "start_line",
    "end_line",
    "lang",
    "part",
    "parts",
    "header",
    "content",
    "search_text",
    "path_text",
    "content_hash",
    "tokens",
)


@dataclass(frozen=True, slots=True)
class FileChunks:
    path: str
    language: str | None
    chunks: Sequence[Chunk]


def _text(value: str) -> str:
    return value.replace("\x00", "�")  # Postgres text can't hold NUL


async def prune_chunks(db: AsyncSession, repo_id: uuid.UUID, *, keep: uuid.UUID) -> int:
    """Delete the chunks of the repository's snapshots other than ``keep`` (superseded by it),
    which are no longer searchable; returns how many rows went. Left in place, they would also
    weigh in the partition's BM25 statistics."""
    await db.execute(
        update(Snapshot)
        .where(Snapshot.repo_id == repo_id, Snapshot.id != keep, Snapshot.chunk_count.is_not(None))
        .values(chunk_count=None)
    )
    result = await db.execute(
        delete(CodeChunk).where(CodeChunk.repo_id == repo_id, CodeChunk.snapshot_id != keep)
    )
    return int(cast("Any", result).rowcount or 0)


async def delete_owned_chunks(db: AsyncSession, owner_file_ids: Sequence[uuid.UUID]) -> None:
    """Delete the chunks these files own (before they're chunked again)."""
    if owner_file_ids:
        await db.execute(delete(CodeChunk).where(CodeChunk.owner_file_id.in_(owner_file_ids)))


class ChunkWriter:
    """Writes a snapshot's chunks file by file (:meth:`add`), flushing every
    :data:`BATCH_ROWS` rows; :meth:`finish` flushes the rest and returns how many were written.
    Files whose path isn't in ``file_ids`` (not files of the snapshot) are passed over."""

    def __init__(
        self,
        db: AsyncSession,
        snapshot: Snapshot,
        file_ids: Mapping[str, uuid.UUID],
        *,
        replace_all: bool = True,
        batch_rows: int = BATCH_ROWS,
    ) -> None:
        self._db = db
        self._snapshot = snapshot
        self._file_ids = file_ids
        self._replace_all = replace_all
        self._batch_rows = batch_rows
        self._records: list[tuple[Any, ...]] = []
        self._owners: list[uuid.UUID] = []
        self._started = False
        self._staging = False
        self.written = 0

    async def add(self, file: FileChunks) -> None:
        if not self._started:
            await self._start()
        file_id = self._file_ids.get(file.path)
        if file_id is None:
            return
        self._owners.append(file_id)
        snapshot = self._snapshot
        for chunk in file.chunks:
            self._records.append(
                (
                    uuid7(),
                    snapshot.org_id,
                    snapshot.repo_id,
                    snapshot.id,
                    file_id,
                    file_id,
                    None if chunk.symbol_key is None else _text(chunk.symbol_key),
                    chunk.view,
                    chunk.kind.value,
                    _text(chunk.path),
                    chunk.start_line,
                    chunk.end_line,
                    file.language,
                    None if chunk.part is None else chunk.part[0],
                    None if chunk.part is None else chunk.part[1],
                    _text(chunk.header),
                    _text(chunk.content),
                    _text(chunk.search_text),
                    _text(chunk.path_text),
                    chunk.content_hash,
                    chunk.tokens,
                )
            )
        if len(self._records) >= self._batch_rows:
            await self._flush()

    async def finish(self) -> int:
        if not self._started:
            await self._start()
        await self._flush()
        if self._staging:
            await self._driver_execute(f"DROP TABLE {STAGING_TABLE}")
            self._staging = False
        return self.written

    async def _start(self) -> None:
        self._started = True
        if self._replace_all:
            snapshot_id = self._snapshot.id
            await self._db.execute(delete(CodeChunk).where(CodeChunk.snapshot_id == snapshot_id))

    async def _driver(self) -> Any:
        connection = await self._db.connection()
        raw = await connection.get_raw_connection()
        return cast("Any", raw.driver_connection)  # asyncpg.Connection

    async def _driver_execute(self, statement: str) -> None:
        await (await self._driver()).execute(statement)

    async def _flush(self) -> None:
        if not self._replace_all:
            await delete_owned_chunks(self._db, self._owners)
        self._owners = []
        if not self._records:
            return
        driver = await self._driver()
        if not self._staging:
            await driver.execute(
                f"CREATE TEMPORARY TABLE {STAGING_TABLE} "
                "(LIKE chunks INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            self._staging = True
        await driver.copy_records_to_table(
            STAGING_TABLE, records=self._records, columns=list(COLUMNS)
        )
        columns = ", ".join(COLUMNS)
        selected = ", ".join(f"s.{c}" for c in COLUMNS)
        # A definition's chunks point at its node, found by key in the same snapshot.
        await driver.execute(
            f"INSERT INTO chunks ({columns}, symbol_id) "  # noqa: S608 - constant names
            f"SELECT {selected}, n.id FROM {STAGING_TABLE} s "
            "LEFT JOIN nodes n ON n.snapshot_id = s.snapshot_id AND n.symbol_key = s.symbol_key"
        )
        await driver.execute(f"TRUNCATE {STAGING_TABLE}")
        self.written += len(self._records)
        self._records = []


async def save_chunks(
    db: AsyncSession,
    snapshot: Snapshot,
    file_ids: Mapping[str, uuid.UUID],
    files: Iterable[FileChunks],
    *,
    replace_all: bool = True,
) -> int:
    """Write the chunks of ``files`` (see :class:`ChunkWriter`); returns the rows written."""
    writer = ChunkWriter(db, snapshot, file_ids, replace_all=replace_all)
    for file in files:
        await writer.add(file)
    return await writer.finish()
