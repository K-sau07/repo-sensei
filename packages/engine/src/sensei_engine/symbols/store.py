"""Saving a snapshot's symbols as ``nodes`` rows.

Rows go in with ``COPY`` into a temporary table, then ``INSERT … SELECT … ON CONFLICT`` (the
``nodes`` row-level security policy checks every row: they can only land in the session's org;
``COPY`` itself isn't allowed into a table with a policy). Saving again replaces rather than adds.
A symbol the snapshot already had (by its key) is updated in place and keeps its id, so what
points at it (chunks, the glossary, graph edges from other files) still does. For a full index
the snapshot's other symbols are then deleted; an incremental run passes only the files it
re-extracted, and only their symbols that are gone are deleted.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

import orjson
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from sensei_core.models import SYMBOL_KINDS, CodeNode, Snapshot
from sensei_engine.symbols.extract import Symbol

COLUMNS = (
    "id",
    "org_id",
    "repo_id",
    "snapshot_id",
    "kind",
    "symbol_key",
    "name",
    "qualified_name",
    "file_id",
    "start_line",
    "end_line",
    "start_col",
    "end_col",
    "lang",
    "signature",
    "docstring",
    "attrs",
    "content_hash",
    "owner_file_id",
)
UPDATED = tuple(c for c in COLUMNS if c not in ("id", "org_id", "snapshot_id", "symbol_key"))
STAGING_TABLE = "nodes_staging"


@dataclass(frozen=True, slots=True)
class FileSymbols:
    path: str
    language: str
    symbols: list[Symbol]


def _text(value: str) -> str:
    return value.replace("\x00", "�")  # Postgres text can't hold NUL


def _maybe(value: str | None) -> str | None:
    return None if value is None else _text(value)


async def save_symbols(
    db: AsyncSession,
    snapshot: Snapshot,
    file_ids: Mapping[str, uuid.UUID],
    files: Iterable[FileSymbols],
    *,
    replace_all: bool = True,
) -> int:
    """Write the symbols of ``files`` (paths found in ``file_ids``, the snapshot's file rows);
    returns the rows written. ``replace_all``: the snapshot's other symbols go too (a full
    index). Runs in the caller's transaction, which must have the snapshot's org set."""
    present = [(file, file_id) for file in files if (file_id := file_ids.get(file.path))]
    owners = [file_id for _, file_id in present]  # others aren't files of this snapshot
    written = sum(len(file.symbols) for file, _ in present)

    def records() -> Iterator[tuple[Any, ...]]:
        # Made as COPY sends them: the symbols aren't held twice.
        for file, file_id in present:
            for symbol in file.symbols:
                yield (
                    uuid7(),
                    snapshot.org_id,
                    snapshot.repo_id,
                    snapshot.id,
                    symbol.kind.value,
                    _text(symbol.symbol_key),
                    _text(symbol.name),
                    _text(symbol.qualified_name),
                    file_id,
                    symbol.start_line,
                    symbol.end_line,
                    symbol.start_col,
                    symbol.end_col,
                    file.language,
                    _maybe(symbol.signature),
                    _maybe(symbol.docstring),
                    orjson.dumps(symbol.attrs).decode().replace("\\u0000", "\\ufffd"),
                    symbol.content_hash,
                    file_id,
                )

    if not written:  # nothing to keep: the files' symbols (or the snapshot's) all go
        scope = (
            CodeNode.snapshot_id == snapshot.id
            if replace_all
            else CodeNode.owner_file_id.in_(owners)
        )
        if replace_all or owners:
            await db.execute(delete(CodeNode).where(scope, CodeNode.kind.in_(SYMBOL_KINDS)))
    if written:
        connection = await db.connection()
        raw = await connection.get_raw_connection()
        driver = cast("Any", raw.driver_connection)  # asyncpg.Connection
        columns = ", ".join(COLUMNS)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in UPDATED)
        # A definition unchanged since it was saved isn't written again.
        changed = (
            f"({', '.join(f'n.{c}' for c in UPDATED)}) IS DISTINCT FROM "
            f"({', '.join(f'EXCLUDED.{c}' for c in UPDATED)})"
        )
        await driver.execute(
            f"CREATE TEMPORARY TABLE {STAGING_TABLE} (LIKE public.nodes INCLUDING DEFAULTS) "
            "ON COMMIT DROP"
        )
        await driver.copy_records_to_table(STAGING_TABLE, records=records(), columns=list(COLUMNS))
        await driver.execute(
            f"INSERT INTO public.nodes AS n ({columns}) SELECT {columns} FROM {STAGING_TABLE} "  # noqa: S608 - constant names
            f"ON CONFLICT (snapshot_id, symbol_key) DO UPDATE SET {updates} WHERE {changed}"
        )
        # The symbols that are gone: the snapshot's (a full index) or the files' (incremental).
        scope = "n.snapshot_id = $1" if replace_all else "n.owner_file_id = ANY($1::uuid[])"
        await driver.execute(
            f"DELETE FROM public.nodes n WHERE {scope} AND n.kind = ANY($2) "  # noqa: S608 - constant names
            f"AND NOT EXISTS (SELECT FROM {STAGING_TABLE} s WHERE s.symbol_key = n.symbol_key)",
            snapshot.id if replace_all else owners,
            list(SYMBOL_KINDS),  # the graph's nodes are the graph writer's
        )
        await driver.execute(f"DROP TABLE {STAGING_TABLE}")
    return written
