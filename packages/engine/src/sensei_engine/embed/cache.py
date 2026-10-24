"""Document embeddings by content hash: each chunk's text is embedded once per model and size, in
any repository or snapshot, and read back from ``embedding_cache`` every other time.

:func:`embed_documents` looks the hashes up, embeds only the misses (each distinct hash once),
stores them and returns every vector. Queries are never cached here: a query's vector differs
from a document's for the same text.

Vectors come back as stored, in half precision (about 3 significant digits), whether they were
just embedded or read from the cache, so a chunk gets the same vector either way.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import batched

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import EmbeddingCache
from sensei_core.models.embeddings import CONTENT_HASH_BYTES
from sensei_engine.embed.provider import EmbeddingProvider, InputType, UsageSink, model_key

LOOKUP_BATCH = 1000  # hashes per query
STORE_BATCH = 500  # rows per insert


@dataclass(slots=True)
class CachedEmbeddings:
    vectors: dict[bytes, list[float]] = field(default_factory=dict[bytes, list[float]])
    hits: int = 0  # distinct hashes read from the cache
    embedded: int = 0  # distinct hashes embedded now


def half_precision(vector: Sequence[float]) -> list[float]:
    """``vector`` as ``halfvec`` stores it (IEEE 754 half precision, rounded to nearest): what
    Postgres prints for a ``halfvec`` reads back to these same floats."""
    packed = struct.pack(f"<{len(vector)}e", *vector)
    return list(struct.unpack(f"<{len(vector)}e", packed))


async def lookup(
    db: AsyncSession, model: str, dims: int, hashes: Sequence[bytes]
) -> dict[bytes, list[float]]:
    """The cached vectors among ``hashes``, for ``model`` (``provider/model``) at ``dims``."""
    found: dict[bytes, list[float]] = {}
    for chunk in batched(dict.fromkeys(hashes), LOOKUP_BATCH, strict=False):
        rows = await db.execute(
            select(EmbeddingCache.content_hash, EmbeddingCache.embedding).where(
                EmbeddingCache.model == model,
                EmbeddingCache.dims == dims,
                EmbeddingCache.content_hash.in_(chunk),
            )
        )
        found.update(
            {content_hash: half_precision(embedding) for content_hash, embedding in rows.tuples()}
        )
    return found


async def store(
    db: AsyncSession, model: str, dims: int, vectors: Mapping[bytes, Sequence[float]]
) -> None:
    """Add ``vectors`` to the cache; a hash already there keeps its row."""
    rows = [
        {"content_hash": key, "model": model, "dims": dims, "embedding": vector}
        for key, vector in vectors.items()
    ]
    for chunk in batched(rows, STORE_BATCH, strict=False):
        statement = insert(EmbeddingCache).values(list(chunk)).on_conflict_do_nothing()
        await db.execute(statement)


async def embed_documents(
    db: AsyncSession,
    provider: EmbeddingProvider,
    items: Sequence[tuple[bytes, str]],
    *,
    on_usage: UsageSink | None = None,
) -> CachedEmbeddings:
    """A vector for each ``(content hash, text)``: from the cache where it has one, embedded
    (as documents) and cached otherwise."""
    texts: dict[bytes, str] = {}
    for content_hash, text in items:
        if len(content_hash) != CONTENT_HASH_BYTES:
            msg = (
                f"content hashes are SHA-256 ({CONTENT_HASH_BYTES} bytes), got {len(content_hash)}"
            )
            raise ValueError(msg)
        texts.setdefault(content_hash, text)
    model, dims = model_key(provider), provider.dims
    result = CachedEmbeddings(vectors=await lookup(db, model, dims, list(texts)))
    result.hits = len(result.vectors)
    misses = [content_hash for content_hash in texts if content_hash not in result.vectors]
    if misses:
        embedded = await provider.embed(
            [texts[content_hash] for content_hash in misses],
            InputType.DOCUMENT,
            on_usage=on_usage,
        )
        fresh = {
            content_hash: half_precision(vector)
            for content_hash, vector in zip(misses, embedded, strict=True)
        }
        await store(db, model, dims, fresh)
        result.vectors.update(fresh)
        result.embedded = len(fresh)
    return result
