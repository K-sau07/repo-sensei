"""Chunks: the retrieval units of a snapshot (see :mod:`sensei_engine.chunk`).

The table is partitioned by ``HASH (repo_id)`` into :data:`CHUNK_PARTITIONS` partitions, made by
the migration (they aren't models). Writes and most reads go through ``chunks``; keyword search
reads the repository's partition directly, with that partition's two BM25 indexes
(pg_textsearch, ``simple`` text config: :func:`bm25_index`): ``search_text`` (the ``lex_code``
leg: header and code as index terms) and ``path_text`` (``lex_path``: path, signature and
name). They are per partition because pg_textsearch scores a query on a partitioned table's
index with the first partition's statistics. BM25 statistics are thus a partition's (every
repository hashed there). Every partition has the org isolation policy too.

``view`` is ``code`` (the chunk itself); 2.8 adds ``summary`` rows for the same symbols.
``generation`` is the run of the chunks stage that wrote the row: search reads a snapshot's live
generation only (``snapshots.chunk_generation``; see :mod:`sensei_engine.chunk.store`).
``symbol_id`` links a definition's chunks to its node (set to null if that node goes; the
``symbol_key`` stays). ``embedding`` is filled by 2.7.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, new_id
from sensei_core.db.tenancy import OrgScopedMixin
from sensei_core.db.types import HalfVector

CHUNK_PARTITIONS = 16
EMBEDDING_DIMENSIONS = 1024


def chunk_partitions() -> list[str]:
    """The names of the partitions of ``chunks``, in order of their hash remainder."""
    return [f"chunks_p{remainder:02}" for remainder in range(CHUNK_PARTITIONS)]


def bm25_index(partition: str, column: str) -> str:
    """The BM25 index on ``column`` (``search_text`` or ``path_text``) of a partition."""
    return f"{partition}_{column}_bm25"


class CodeChunk(OrgScopedMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        # A snapshot's chunks of a view and kind (every search filters on these first).
        Index("ix_chunks_repo_snapshot_view_kind", "repo_id", "snapshot_id", "view", "kind"),
        # Deleted with their file (a changed file's chunks are replaced), unlinked from a node.
        Index("ix_chunks_file_id", "file_id"),
        Index("ix_chunks_owner_file_id", "owner_file_id"),
        Index("ix_chunks_symbol_id", "symbol_id"),
        # With org_id: a chunk can't point at another org's repository, snapshot, file or node.
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "org_id"], ["snapshots.id", "snapshots.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["file_id", "org_id"], ["files.id", "files.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["owner_file_id", "org_id"], ["files.id", "files.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["symbol_id", "org_id"],
            ["nodes.id", "nodes.org_id"],
            ondelete="SET NULL (symbol_id)",
        ),
        {"postgresql_partition_by": "HASH (repo_id)"},
    )

    # The partition key is part of the primary key, as Postgres requires.
    repo_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=new_id, server_default=text("uuidv7()")
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column()
    generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    file_id: Mapped[uuid.UUID] = mapped_column()
    owner_file_id: Mapped[uuid.UUID] = mapped_column()  # deleted and rebuilt with this file
    symbol_id: Mapped[uuid.UUID | None] = mapped_column()
    symbol_key: Mapped[str | None] = mapped_column(Text)
    view: Mapped[str] = mapped_column(String(16), server_default="code")
    # function, method, class, component, hook, variable, module, code, doc, sql,
    # openapi_operation, config, text
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)  # 1-based, inclusive
    end_line: Mapped[int] = mapped_column(Integer)
    lang: Mapped[str | None] = mapped_column(String(32))  # the file's language id
    part: Mapped[int | None] = mapped_column(Integer)  # k of n, for a split definition
    parts: Mapped[int | None] = mapped_column(Integer)
    header: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)  # what a model reads
    search_text: Mapped[str] = mapped_column(Text)  # BM25: header, content, split identifiers
    path_text: Mapped[str] = mapped_column(Text)  # BM25: path, signature, name
    content_hash: Mapped[bytes] = mapped_column(LargeBinary)  # SHA-256 of header and content
    tokens: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(HalfVector(EMBEDDING_DIMENSIONS))
