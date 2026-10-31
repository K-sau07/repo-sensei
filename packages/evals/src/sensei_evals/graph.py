"""The eval index's graph (3.6, 3.12), for the hybrid retriever's graph leg (``--opt graph=1``)
and the Ask agent's traces in answer evals.

The snapshot is linked in this process as the graph and links stages do it
(:mod:`sensei_engine.graph.stage`, :mod:`sensei_engine.link.stage`): every file of a language a
resolver reads parsed again and its facts extracted in the pool, the project read from the eval
database, every resolver run and the snapshot's code layer replaced; then the cross-layer facts
(endpoints and their handlers, client calls and the endpoints they target, tables) the same way,
into its links layer, without the sandbox's running app. Then its graph is read into memory. No
model is called.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select

from sensei_core.models import RepoFile, Snapshot
from sensei_engine.graph.project import Project
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.graph.stage import (
    extract_files,
    load_project,
    resolve_graph,
    resolver_for,
    write_graph,
)
from sensei_engine.ingest.classify import Flag
from sensei_engine.link import http_calls
from sensei_engine.link.stage import (
    env_files,
    extract_links,
    links_language,
    resolve_links,
    write_links,
)
from sensei_engine.parse.stage import read_source
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
        every = [(path, language, flags) for path, language, flags in rows]
        readable = [
            (path, language)
            for path, language, flags in every
            if language is not None and not UNREAD_FLAGS.intersection(flags or ())
        ]
        files = [(path, language) for path, language in readable if resolver_for(language)]
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
        paths = [path for path, _, _ in every]
        to_link = [(path, language) for path, language in readable if links_language(language)]
        linked = {f.path: f for f in extract_links(checkout, to_link, _envs(checkout, paths))}
        await write_links(db, snapshot, resolve_links(project, linked))
        return await GraphSnapshot.build(db, snapshot_id)


def _envs(root: Path, paths: list[str]) -> dict[str, dict[str, str]]:
    """The ``.env`` values by folder, as the links stage reads them (one that can't be read is
    left out)."""
    texts: dict[str, str] = {}
    for path in env_files(paths):
        try:
            texts[path] = read_source(root, path).decode("utf-8", "replace")
        except OSError:
            continue
    return http_calls.env_values(Project(frozenset(paths)), texts)
