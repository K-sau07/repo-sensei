"""Chunk embeddings in ``chunks``: the snapshot's chunks still without a vector
(:func:`pending`), their vectors written back (:func:`save`), and the HNSW index of the
repository's partition, built once its first vectors are in (:func:`build_index`).

A chunk is embedded as it was hashed: its header, a line break, its content
(:func:`chunk_text`), so a vector cached under a content hash is the vector of that text.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import CodeChunk
from sensei_core.models.chunks import chunk_partitions

PARTITIONS = frozenset(chunk_partitions())


@dataclass(frozen=True, slots=True)
class PendingChunk:
    id: uuid.UUID
    content_hash: bytes
    text: str  # what is embedded (:func:`chunk_text`)


def chunk_text(header: str, content: str) -> str:
    """The text a chunk's content hash covers, and the text it is embedded as."""
    return f"{header}\n{content}"


async def count_pending(db: AsyncSession, snapshot_id: uuid.UUID) -> int:
    """How many of the snapshot's chunks (every view) have no vector yet."""
    found = await db.scalar(
        select(func.count())
        .select_from(CodeChunk)
        .where(CodeChunk.snapshot_id == snapshot_id, CodeChunk.embedding.is_(None))
    )
    return int(found or 0)


async def pending(
    db: AsyncSession, snapshot_id: uuid.UUID, *, after: uuid.UUID | None = None, limit: int
) -> list[PendingChunk]:
    """Up to ``limit`` of the snapshot's chunks without a vector, in id order after ``after``."""
    query = (
        select(CodeChunk.id, CodeChunk.content_hash, CodeChunk.header, CodeChunk.content)
        .where(CodeChunk.snapshot_id == snapshot_id, CodeChunk.embedding.is_(None))
        .order_by(CodeChunk.id)
        .limit(limit)
    )
    if after is not None:
        query = query.where(CodeChunk.id > after)
    rows = await db.execute(query)
    return [
        PendingChunk(chunk_id, content_hash, chunk_text(header, content))
        for chunk_id, content_hash, header, content in rows.tuples()
    ]


async def save(
    db: AsyncSession, repo_id: uuid.UUID, vectors: Mapping[uuid.UUID, Sequence[float]]
) -> int:
    """Write each chunk's vector (by primary key: the repository's partition only)."""
    if not vectors:
        return 0
    rows = [
        {"repo_id": repo_id, "id": chunk_id, "embedding": list(vector)}
        for chunk_id, vector in sorted(vectors.items())
    ]
    await db.execute(update(CodeChunk), rows)
    return len(rows)


async def build_index(db: AsyncSession, partition: str) -> bool:
    """Build ``partition``'s HNSW index if it has none (whether it did). ``partition``:
    ``chunks_pNN``, or schema-qualified as :func:`~sensei_engine.search.bm25.chunk_partition`
    names it (the function builds in the tables' schema). The function runs as
    the tables' owner; the session's role must be the system role (or the owner). It takes a
    lock that blocks writes to the partition until the transaction ends: commit right after.

    An index built over rows updated since they were vacuumed (vectors just written) can't be
    used by transactions that started before it was built (``pg_index.indcheckxmin``): searches
    use it once those are over."""
    name = partition.rpartition(".")[2]
    if name not in PARTITIONS:
        msg = f"not a partition of chunks: {partition!r}"
        raise ValueError(msg)
    built = await db.scalar(text("SELECT chunk_embedding_index(:partition)"), {"partition": name})
    return bool(built)
