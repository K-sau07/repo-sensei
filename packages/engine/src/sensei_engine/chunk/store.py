"""Saving a snapshot's chunks as ``chunks`` rows, in batches as they're made.

Rows go in with ``COPY`` into a temporary table, :data:`BATCH_ROWS` at a time, then
``INSERT … SELECT`` into ``chunks`` (where row-level security checks every row: they can only land
in the session's org). The insert links each definition's chunks to its node by symbol key, so
the snapshot's symbols are saved first.

**Generations.** Each full chunking of a snapshot writes its rows under a new generation
(:func:`new_generation`: from a sequence, so two runs never share one). Search reads only the
snapshot's live generation (``snapshots.chunk_generation``, set with ``chunk_count``), so rows
of a run still writing, or of one that stopped, are never seen. A :class:`ChunkWriter` given a
session factory commits every batch in its own transaction (no transaction lasts the whole
stage, so VACUUM, DDL and the rest of the database don't wait on it); :func:`make_live` then
makes the generation live and deletes the snapshot's other rows, in one short transaction, and
:func:`discard_generation` deletes the rows of a run that didn't finish. Given a session, the
writer writes in the caller's transaction and makes its generation live at :meth:`finish`.

An incremental save (``replace_all=False``) adds to the live generation, deleting the chunks owned
by each file it saves first (see :func:`delete_owned_chunks`). Once a snapshot is the
repository's indexed one, the other snapshots' chunks are pruned, and its own rows of other
generations swept (:func:`prune_chunks`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import delete, exists, or_, select, text, update
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
    "generation",
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

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class SnapshotKey:
    """A snapshot by its ids (a writer committing batches holds no row of it)."""

    org_id: uuid.UUID
    repo_id: uuid.UUID
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FileChunks:
    path: str
    language: str | None
    chunks: Sequence[Chunk]


def _text(value: str) -> str:
    return value.replace("\x00", "�")  # Postgres text can't hold NUL


def _rowcount(result: object) -> int:
    return int(cast("Any", result).rowcount or 0)


async def new_generation(db: AsyncSession) -> int:
    """A generation no run has used."""
    return int(await db.scalar(text("SELECT nextval('chunk_generations')")) or 0)


async def make_live(db: AsyncSession, snapshot: Snapshot, generation: int, count: int) -> int:
    """Make ``generation`` (``count`` chunks) the snapshot's live one and delete its rows of
    every other; returns how many went. In the caller's transaction."""
    snapshot.chunk_generation = generation
    snapshot.chunk_count = count
    await db.flush()
    result = await db.execute(
        delete(CodeChunk).where(
            CodeChunk.repo_id == snapshot.repo_id,
            CodeChunk.snapshot_id == snapshot.id,
            CodeChunk.generation != generation,
        )
    )
    return _rowcount(result)


async def discard_generation(
    db: AsyncSession, repo_id: uuid.UUID, snapshot_id: uuid.UUID, generation: int
) -> int:
    """Delete the rows a run wrote under ``generation``, unless it's live; returns how many."""
    live = exists().where(Snapshot.id == snapshot_id, Snapshot.chunk_generation == generation)
    result = await db.execute(
        delete(CodeChunk).where(
            CodeChunk.repo_id == repo_id,
            CodeChunk.snapshot_id == snapshot_id,
            CodeChunk.generation == generation,
            ~live,
        )
    )
    return _rowcount(result)


async def prune_chunks(db: AsyncSession, repo_id: uuid.UUID, *, keep: uuid.UUID) -> int:
    """Delete the chunks of the repository's snapshots other than ``keep`` (superseded by it),
    which are no longer searchable, and ``keep``'s rows outside its live generation (a run that
    stopped, or one still writing for a cancelled job); returns how many rows went. Left in
    place, they would also weigh in the partition's BM25 statistics."""
    await db.execute(
        update(Snapshot)
        .where(
            Snapshot.repo_id == repo_id,
            Snapshot.id != keep,
            or_(Snapshot.chunk_count.is_not(None), Snapshot.chunk_generation.is_not(None)),
        )
        .values(chunk_count=None, chunk_generation=None)
    )
    live = select(Snapshot.chunk_generation).where(Snapshot.id == keep).scalar_subquery()
    result = await db.execute(
        delete(CodeChunk).where(
            CodeChunk.repo_id == repo_id,
            or_(CodeChunk.snapshot_id != keep, CodeChunk.generation.is_distinct_from(live)),
        )
    )
    return _rowcount(result)


async def delete_owned_chunks(db: AsyncSession, owner_file_ids: Sequence[uuid.UUID]) -> None:
    """Delete the chunks these files own (before they're chunked again)."""
    if owner_file_ids:
        await db.execute(delete(CodeChunk).where(CodeChunk.owner_file_id.in_(owner_file_ids)))


async def _insert(db: AsyncSession, records: Sequence[tuple[Any, ...]]) -> None:
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    driver = cast("Any", raw.driver_connection)  # asyncpg.Connection
    await driver.execute(
        f"CREATE TEMPORARY TABLE IF NOT EXISTS {STAGING_TABLE} "
        "(LIKE chunks INCLUDING DEFAULTS) ON COMMIT DROP"
    )
    await driver.copy_records_to_table(STAGING_TABLE, records=records, columns=list(COLUMNS))
    columns = ", ".join(COLUMNS)
    selected = ", ".join(f"s.{c}" for c in COLUMNS)
    # A definition's chunks point at its node, found by key in the same snapshot.
    await driver.execute(
        f"INSERT INTO chunks ({columns}, symbol_id) "  # noqa: S608 - constant names
        f"SELECT {selected}, n.id FROM {STAGING_TABLE} s "
        "LEFT JOIN nodes n ON n.snapshot_id = s.snapshot_id AND n.symbol_key = s.symbol_key"
    )
    await driver.execute(f"DROP TABLE {STAGING_TABLE}")


class ChunkWriter:
    """Writes a snapshot's chunks file by file (:meth:`add`), :data:`BATCH_ROWS` rows at a time;
    :meth:`finish` writes the rest and returns how many were written. Files whose path isn't in
    ``file_ids`` (not files of the snapshot) are passed over.

    ``db`` is a session (everything in the caller's transaction; a full save makes its
    generation live at :meth:`finish`) or a session factory (each batch committed in its own
    transaction, under ``generation``, which the caller makes live: see the module)."""

    def __init__(
        self,
        db: AsyncSession | Sessions,
        snapshot: Snapshot | SnapshotKey,
        file_ids: Mapping[str, uuid.UUID],
        *,
        generation: int | None = None,
        replace_all: bool = True,
        batch_rows: int = BATCH_ROWS,
    ) -> None:
        self._session = db if isinstance(db, AsyncSession) else None
        self._sessions = None if isinstance(db, AsyncSession) else db
        if self._sessions is not None and (generation is None or not replace_all):
            msg = "a writer committing its batches writes a full generation it's given"
            raise ValueError(msg)
        self._snapshot = snapshot
        self._ids = (snapshot.org_id, snapshot.repo_id, snapshot.id)
        self._file_ids = file_ids
        self.generation = generation
        self._replace_all = replace_all
        self._batch_rows = batch_rows
        self._records: list[tuple[Any, ...]] = []
        self._owners: list[uuid.UUID] = []
        self._started = False
        self.written = 0

    async def add(self, file: FileChunks) -> None:
        if not self._started:
            await self._start()
        file_id = self._file_ids.get(file.path)
        if file_id is None:
            return
        self._owners.append(file_id)
        org_id, repo_id, snapshot_id = self._ids
        for chunk in file.chunks:
            self._records.append(
                (
                    uuid7(),
                    org_id,
                    repo_id,
                    snapshot_id,
                    self.generation,
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
        db = self._session
        if db is not None and self._replace_all and self.generation is not None:
            snapshot = await db.get(Snapshot, self._ids[2])
            if snapshot is not None:
                await make_live(db, snapshot, self.generation, self.written)
        return self.written

    async def _start(self) -> None:
        self._started = True
        db = self._session
        if db is None or self.generation is not None:
            return
        if self._replace_all:
            self.generation = await new_generation(db)
        else:  # added to the live generation
            live = await db.scalar(
                select(Snapshot.chunk_generation).where(Snapshot.id == self._ids[2])
            )
            self.generation = int(live or 0)

    async def _flush(self) -> None:
        records, self._records = self._records, []
        owners, self._owners = self._owners, []
        if self._session is not None:
            if not self._replace_all:
                await delete_owned_chunks(self._session, owners)
            if records:
                await _insert(self._session, records)
        elif records and self._sessions is not None:
            async with self._sessions() as db:
                await _insert(db, records)
        self.written += len(records)


async def save_chunks(
    db: AsyncSession,
    snapshot: Snapshot,
    file_ids: Mapping[str, uuid.UUID],
    files: Iterable[FileChunks],
    *,
    replace_all: bool = True,
) -> int:
    """Write the chunks of ``files`` in the caller's transaction (see :class:`ChunkWriter`);
    returns the rows written."""
    writer = ChunkWriter(db, snapshot, file_ids, replace_all=replace_all)
    for file in files:
        await writer.add(file)
    return await writer.finish()
