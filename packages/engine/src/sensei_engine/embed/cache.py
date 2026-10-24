"""Document embeddings by content hash, per org: each chunk's text is embedded once per org,
model and size, in any of the org's repositories or snapshots, and read back from
``embedding_cache`` every other time. Orgs never share vectors (see the model).

:func:`embed_documents` looks the hashes up, embeds only the misses (each distinct hash once),
stores them and returns every vector. Misses go to the provider :data:`EMBED_GROUP` at a time;
each group is stored (with its usage) as soon as it comes back, then ``checkpoint`` runs: a
caller that commits there keeps what was paid for when a later group fails, and the next run
finds it in the cache. At most the group in flight is lost (billed, not stored). Queries are
never cached here: a query's vector differs from a document's for the same text.

Vectors come back as stored, in half precision (about 3 significant digits), whether they were
just embedded or read from the cache, so a chunk gets the same vector either way.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import batched

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.types import half_precision
from sensei_core.models import EmbeddingCache
from sensei_core.models.embeddings import CONTENT_HASH_BYTES
from sensei_engine.embed.provider import EmbeddingProvider, InputType, UsageSink, model_key

LOOKUP_BATCH = 1000  # hashes per query
STORE_BATCH = 500  # rows per insert
EMBED_GROUP = 1000  # misses per provider call; each group is stored before the next is sent


@dataclass(slots=True)
class CachedEmbeddings:
    vectors: dict[bytes, list[float]] = field(default_factory=dict[bytes, list[float]])
    hits: int = 0  # distinct hashes read from the cache
    embedded: int = 0  # distinct hashes embedded now


async def lookup(
    db: AsyncSession, org_id: uuid.UUID, model: str, dims: int, hashes: Sequence[bytes]
) -> dict[bytes, list[float]]:
    """The org's cached vectors among ``hashes``, for ``model`` (``provider/model``) at
    ``dims``."""
    found: dict[bytes, list[float]] = {}
    for chunk in batched(dict.fromkeys(hashes), LOOKUP_BATCH, strict=False):
        rows = await db.execute(
            select(EmbeddingCache.content_hash, EmbeddingCache.embedding).where(
                EmbeddingCache.org_id == org_id,
                EmbeddingCache.model == model,
                EmbeddingCache.dims == dims,
                EmbeddingCache.content_hash.in_(chunk),
            )
        )
        # Not dict(rows.tuples()): a result has keys(), so dict() would read it as a mapping.
        found.update({h: vector for h, vector in rows.tuples()})  # noqa: C416
    return found


async def store(
    db: AsyncSession,
    org_id: uuid.UUID,
    model: str,
    dims: int,
    vectors: Mapping[bytes, Sequence[float]],
) -> None:
    """Add ``vectors`` to the org's cache; a hash already there keeps its row. Rows go in hash
    order, so two overlapping inserts at once take their locks in the same order (no
    deadlock)."""
    rows = [
        {"org_id": org_id, "content_hash": key, "model": model, "dims": dims, "embedding": vector}
        for key, vector in sorted(vectors.items())
    ]
    for chunk in batched(rows, STORE_BATCH, strict=False):
        statement = insert(EmbeddingCache).values(list(chunk)).on_conflict_do_nothing()
        await db.execute(statement)


async def embed_documents(
    db: AsyncSession,
    provider: EmbeddingProvider,
    items: Sequence[tuple[bytes, str]],
    *,
    org_id: uuid.UUID,
    on_usage: UsageSink | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> CachedEmbeddings:
    """A vector for each ``(content hash, text)`` of the org's: from the cache where it has one,
    embedded (as documents) and cached otherwise. ``checkpoint`` runs after each group of misses
    is stored (commit there to keep it)."""
    texts: dict[bytes, str] = {}
    for content_hash, text in items:
        if len(content_hash) != CONTENT_HASH_BYTES:
            msg = (
                f"content hashes are SHA-256 ({CONTENT_HASH_BYTES} bytes), got {len(content_hash)}"
            )
            raise ValueError(msg)
        texts.setdefault(content_hash, text)
    model, dims = model_key(provider), provider.dims
    result = CachedEmbeddings(vectors=await lookup(db, org_id, model, dims, list(texts)))
    result.hits = len(result.vectors)
    misses = [content_hash for content_hash in texts if content_hash not in result.vectors]
    for group in batched(misses, EMBED_GROUP, strict=False):
        embedded = await provider.embed(
            [texts[content_hash] for content_hash in group],
            InputType.DOCUMENT,
            on_usage=on_usage,
        )
        fresh = {
            content_hash: half_precision(vector)
            for content_hash, vector in zip(group, embedded, strict=True)
        }
        await store(db, org_id, model, dims, fresh)
        result.vectors.update(fresh)
        result.embedded += len(fresh)
        if checkpoint is not None:
            await checkpoint()
    return result
