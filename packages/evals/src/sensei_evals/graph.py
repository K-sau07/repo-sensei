"""The eval index's code graph (3.6), for the hybrid retriever's graph leg (``--opt graph=1``).

The snapshot is linked in this process as the graph stage does it
(:mod:`sensei_engine.graph.stage`): every file of a language a resolver reads parsed again and
its facts extracted in the pool, the project read from the eval database, every resolver run, and
the snapshot's code layer replaced; then its graph is read into memory. No model is called.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select

from sensei_core.models import RepoFile, Snapshot
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.graph.stage import (
    extract_files,
    load_project,
    resolve_graph,
    resolver_for,
    write_graph,
)
from sensei_engine.ingest.classify import Flag
from sensei_evals.symbols import Sessions

UNREAD_FLAGS = frozenset({Flag.BINARY, Flag.TOO_LARGE, Flag.LFS_POINTER})


async def link_snapshot(
    sessions: Sessions, snapshot_id: uuid.UUID, checkout: Path
) -> GraphSnapshot:
    """Link the snapshot (its files and symbols saved, ``checkout`` its working copy) and return
    its graph."""
    async with sessions() as db:
        rows = await db.execute(
            select(RepoFile.path, RepoFile.language, RepoFile.flags)
            .where(RepoFile.snapshot_id == snapshot_id)
            .order_by(RepoFile.path)
        )
        files = [
            (path, language)
            for path, language, flags in rows
            if language is not None
            and resolver_for(language) is not None
            and not UNREAD_FLAGS.intersection(flags or ())
        ]
        facts: dict[str, dict[str, object]] = {}
        for found in extract_files(checkout, files):
            if found.facts is not None:
                facts.setdefault(found.resolver, {})[found.path] = found.facts
        project = await load_project(db, snapshot_id, checkout)
        snapshot = await db.get(Snapshot, snapshot_id)
        if snapshot is None:  # pragma: no cover - indexed just before
            msg = "the eval snapshot is gone"
            raise RuntimeError(msg)
        await write_graph(db, snapshot, resolve_graph(project, facts))
        return await GraphSnapshot.build(db, snapshot_id)
