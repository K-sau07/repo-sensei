"""The keyword retriever: step 2.6's BM25 legs (:mod:`sensei_engine.search.bm25`) over a
benchmark checkout's chunks.

The checkout is indexed in this process the way the indexer does it, into the eval database
(see :mod:`sensei_evals.symbols`): files and symbols as there, then the chunks stage's chunks
(:func:`sensei_engine.chunk.stage.chunk_files`, saved with the same writer). The question is
searched as it is, on both legs fused (``--opt legs=code`` or ``path`` for one); each chunk is a
hit with its lines.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from sensei_core.config import get_settings
from sensei_core.db import make_database
from sensei_core.models import RepoFile, Snapshot
from sensei_engine.chunk.stage import chunk_files, chunkable
from sensei_engine.chunk.store import ChunkWriter, FileChunks
from sensei_engine.search.bm25 import Leg, LexHit, keyword_search
from sensei_evals.retrieval import Hit, RepoRef, RetrieverContext
from sensei_evals.symbols import EVAL_DATABASE, Sessions, eval_database_url, index_checkout

LEGS: dict[str, tuple[Leg, ...]] = {
    "both": (Leg.CODE, Leg.PATH),
    "code": (Leg.CODE,),
    "path": (Leg.PATH,),
}


async def index_chunks(sessions: Sessions, snapshot_id: uuid.UUID, checkout: Path) -> int:
    """Chunk ``checkout`` into the snapshot (its files and symbols already saved) as the chunks
    stage does; returns how many chunks were saved."""
    async with sessions() as db:
        rows = await db.execute(
            select(RepoFile.path, RepoFile.id, RepoFile.language, RepoFile.flags).where(
                RepoFile.snapshot_id == snapshot_id
            )
        )
        files = {
            path: (file_id, language)
            for path, file_id, language, flags in rows
            if chunkable(path, language, flags or ())
        }
        current = await db.get(Snapshot, snapshot_id)
        if current is None:  # pragma: no cover - indexed just before
            msg = "the eval snapshot is gone"
            raise RuntimeError(msg)
        writer = ChunkWriter(db, current, {path: ids[0] for path, ids in files.items()})
        pairs = sorted((path, language) for path, (_, language) in files.items())
        for chunked in chunk_files(checkout, pairs):
            if chunked.chunks:
                await writer.add(FileChunks(chunked.path, chunked.language, chunked.chunks))
        written = await writer.finish()
        current.chunk_count = written
        return written


def _hit(found: LexHit) -> Hit:
    return Hit(
        path=found.path,
        lines=(found.start_line, found.end_line),
        score=found.score,
        symbol=found.symbol_key,
    )


class KeywordRetriever:
    """BM25 over one snapshot's chunks, both legs fused by reciprocal rank (or one leg).

    With ``checkout``, :meth:`prepare` indexes it first (symbols, then chunks) and calls
    ``analyze``; otherwise ``snapshot_id`` and ``repo_id`` name a chunked snapshot."""

    name = "bm25"

    def __init__(
        self,
        sessions: Sessions,
        *,
        legs: Sequence[Leg] = (Leg.CODE, Leg.PATH),
        snapshot: tuple[uuid.UUID, uuid.UUID] | None = None,
        checkout: Path | None = None,
        close: Callable[[], Awaitable[None]] | None = None,
        analyze: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if snapshot is None and checkout is None:
            msg = "the keyword retriever needs a snapshot or a checkout to index"
            raise ValueError(msg)
        self._sessions = sessions
        self._legs = tuple(legs)
        self._snapshot = snapshot  # (repo id, snapshot id)
        self._checkout = checkout
        self._close = close
        self._analyze = analyze
        self.chunk_count: int | None = None
        self.index_seconds: float | None = None

    async def prepare(self, repo: RepoRef) -> None:
        """Index the checkout, once (the runner calls this before timing any search)."""
        if self._snapshot is not None or self._checkout is None:
            return
        started = time.monotonic()
        async with self._sessions() as db:
            snapshot = await index_checkout(db, repo, self._checkout)
            ids = (snapshot.repo_id, snapshot.id)
        self.chunk_count = await index_chunks(self._sessions, ids[1], self._checkout)
        self._snapshot = ids
        if self._analyze is not None:
            await self._analyze()
        self.index_seconds = time.monotonic() - started

    async def search(self, query: str, repo: RepoRef, k: int) -> list[Hit]:
        await self.prepare(repo)
        if self._snapshot is None:  # pragma: no cover - prepare set it or raised
            return []
        repo_id, snapshot_id = self._snapshot
        async with self._sessions() as db:
            found = await keyword_search(
                db, repo_id=repo_id, snapshot_id=snapshot_id, query=query, k=k, legs=self._legs
            )
        return [_hit(hit) for hit in found]

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()


async def analyze_index(url: str) -> None:
    """Fresh planner statistics for everything the eval index loaded (through the owner)."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("ANALYZE files, nodes, chunks"))
    finally:
        await engine.dispose()


def checkout_keyword(context: RetrieverContext) -> KeywordRetriever:
    """The ``bm25`` retriever of ``sensei-eval run``: the checkout indexed into the eval
    database (``--opt db=<name>``, default :data:`EVAL_DATABASE`), through the system role;
    ``--opt legs=both|code|path``."""
    if context.repo.checkout is None:
        msg = "the bm25 retriever needs a checkout of the repository (--checkout)"
        raise ValueError(msg)
    legs = context.options.get("legs", "both")
    if legs not in LEGS:
        msg = f"legs must be one of {', '.join(LEGS)}, not {legs!r}"
        raise ValueError(msg)
    settings = get_settings().database
    name = context.options.get("db", EVAL_DATABASE)
    url = eval_database_url(settings.system_url.get_secret_value(), name)
    database = make_database(settings, url=url)
    owner_url = eval_database_url(settings.migrate_url.get_secret_value(), name)
    return KeywordRetriever(
        database.session,
        legs=LEGS[legs],
        checkout=context.repo.checkout,
        close=database.dispose,
        analyze=lambda: analyze_index(owner_url),
    )
