"""Snapshots' code graphs in memory (3.6), for the graph endpoints and the search's graph leg.

A snapshot's graph is ready once an index job for it finished its graph stage; that run's
details name the graph's ``version``. The graph is read from the snapshot's ``graph.bin``
artifact when it carries that version, else built from the database (an artifact that is missing,
or older than the rows, or unreadable). Graphs are kept by (snapshot, version), least recently
used out first within a memory budget (each graph's estimate,
:meth:`~sensei_engine.graph.snapshot.GraphSnapshot.memory_bytes`, its nodes counted as well as
its edges): a snapshot indexed again is read anew, never served stale.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.errors import ConflictError
from sensei_core.logging import get_logger
from sensei_core.models import IndexJob, Snapshot, Stage
from sensei_core.storage import Bucket, ObjectStorage, artifact_key
from sensei_engine.graph.snapshot import ARTIFACT_NAME, GraphSnapshot

log = get_logger(__name__)

MB = 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * MB

Key = tuple[uuid.UUID, str]


async def graph_version(db: AsyncSession, snapshot: Snapshot) -> str | None:
    """The version of the snapshot's latest finished graph (None: no graph yet)."""
    graph = IndexJob.stages[Stage.GRAPH.value]
    version = await db.scalar(
        select(graph["details"]["version"].astext)
        .where(IndexJob.snapshot_id == snapshot.id, graph["status"].astext == "done")
        .order_by(IndexJob.created_at.desc())
        .limit(1)
    )
    return version or None


class GraphStore:
    """Graphs by (snapshot, version), within ``max_mb`` (one larger graph is still served)."""

    def __init__(self, *, max_mb: int = 128) -> None:
        self._max_bytes = max_mb * MB
        self._graphs: OrderedDict[Key, GraphSnapshot] = OrderedDict()
        self._sizes: dict[Key, int] = {}
        self._loading: dict[Key, asyncio.Lock] = {}

    def __len__(self) -> int:
        """How many graphs are held."""
        return len(self._graphs)

    async def get(
        self, db: AsyncSession, storage: ObjectStorage, snapshot: Snapshot
    ) -> GraphSnapshot:
        """The snapshot's graph; ``409 graph_not_ready`` when it has none yet."""
        version = await graph_version(db, snapshot)
        if version is None:
            raise ConflictError(
                "The code graph appears once this commit's indexing has linked its files.",
                code="graph_not_ready",
            )
        key = (snapshot.id, version)
        held = self._cached(key)
        if held is not None:
            return held
        lock = self._loading.setdefault(key, asyncio.Lock())
        async with lock:
            held = self._cached(key)  # loaded while this one waited
            if held is not None:
                return held
            try:
                graph = await self._load(db, storage, snapshot, version)
                size = await asyncio.to_thread(graph.memory_bytes)
            finally:
                self._loading.pop(key, None)
        self._keep(key, graph, size)
        return graph

    def _cached(self, key: Key) -> GraphSnapshot | None:
        graph = self._graphs.get(key)
        if graph is not None:
            self._graphs.move_to_end(key)
        return graph

    async def _load(
        self, db: AsyncSession, storage: ObjectStorage, snapshot: Snapshot, version: str
    ) -> GraphSnapshot:
        name = artifact_key(snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, ARTIFACT_NAME)
        try:
            data = await storage.get_bytes(Bucket.ARTIFACTS, name, max_bytes=MAX_ARTIFACT_BYTES)
            graph = await asyncio.to_thread(GraphSnapshot.loads, data) if data else None
        except Exception as exc:  # any bad artifact: the rows are the truth
            log.warning(
                "unreadable graph artifact", snapshot_id=str(snapshot.id), error=type(exc).__name__
            )
            graph = None
        if graph is not None and graph.version == version:
            return graph
        return await GraphSnapshot.build(db, snapshot.id, version=version)

    def _keep(self, key: Key, graph: GraphSnapshot, size: int) -> None:
        self._graphs[key] = graph
        self._sizes[key] = size
        self._graphs.move_to_end(key)
        for other in [k for k in self._graphs if k[0] == key[0] and k != key]:
            self._drop(other)  # the snapshot's older versions
        while len(self._graphs) > 1 and sum(self._sizes.values()) > self._max_bytes:
            self._drop(next(iter(self._graphs)))

    def _drop(self, key: Key) -> None:
        del self._graphs[key]
        del self._sizes[key]
