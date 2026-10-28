"""The graph store keeps to its memory budget, nodes counted as well as edges, and reads a graph
whose artifact is unreadable from the rows instead."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from sensei_api import graphs
from sensei_api.graphs import MB, GraphStore
from sensei_core.models import Snapshot
from sensei_core.storage import ObjectStorage, artifact_key
from sensei_engine.graph.snapshot import (
    ARTIFACT_NAME,
    CODE_NODE_BYTES,
    NODE_BYTES,
    GraphSnapshot,
    Node,
)

VERSION = "v1"


@dataclass
class FakeStorage:
    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])
    reads: list[str] = field(default_factory=list[str])

    async def get_bytes(self, bucket: Any, key: str, *, max_bytes: int) -> bytes | None:
        self.reads.append(key)
        return self.blobs.get(key)


def _snapshot() -> Snapshot:
    return Snapshot(id=uuid.uuid4(), org_id=uuid.uuid4(), repo_id=uuid.uuid4(), commit_sha="a" * 40)


def _key(snapshot: Snapshot) -> str:
    return artifact_key(snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, ARTIFACT_NAME)


def _nodes_only(count: int) -> GraphSnapshot:
    """Definitions and no edges: what counting edges alone took for free."""
    nodes = [
        Node(f"py m.py/f{i}().", "function", f"f{i}", f"f{i}", "m.py", 1, 2, "python")
        for i in range(count)
    ]
    return GraphSnapshot(nodes, [], version=VERSION)


@pytest.fixture(autouse=True)
def _versions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def version(db: Any, snapshot: Snapshot) -> str:
        return VERSION

    monkeypatch.setattr(graphs, "graph_version", version)


async def test_graphs_of_many_nodes_and_no_edges_are_evicted() -> None:
    store = GraphStore(max_mb=4)
    storage = FakeStorage()
    each = 2 * MB // (NODE_BYTES + CODE_NODE_BYTES) - 10  # two of them fit, not three
    snapshots = [_snapshot() for _ in range(3)]
    for snapshot in snapshots:
        storage.blobs[_key(snapshot)] = _nodes_only(each).dumps()
    db = cast("Any", None)
    for snapshot in snapshots[:2]:
        await store.get(db, cast("ObjectStorage", storage), snapshot)
    assert len(store) == 2
    await store.get(db, cast("ObjectStorage", storage), snapshots[2])
    assert len(store) == 2  # the least recently used went
    storage.reads.clear()
    await store.get(db, cast("ObjectStorage", storage), snapshots[0])
    assert storage.reads == [_key(snapshots[0])]  # read again: it was the one evicted


async def test_an_unreadable_artifact_is_built_from_the_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    snapshot = _snapshot()
    blob = _nodes_only(3).dumps()
    built: list[uuid.UUID] = []

    async def build(db: Any, snapshot_id: uuid.UUID, *, version: str = "") -> GraphSnapshot:
        built.append(snapshot_id)
        return _nodes_only(2)

    monkeypatch.setattr(GraphSnapshot, "build", build)
    # Cut short, not zlib at all, zlib of nothing readable.
    for broken in (blob[: len(blob) // 2], b"not zlib", b"x\x9c" + b"\x00" * 8):
        storage.blobs[_key(snapshot)] = broken
        store = GraphStore()
        graph = await store.get(cast("Any", None), cast("ObjectStorage", storage), snapshot)
        assert len(graph) == 2  # the rows', not a 500
    assert built == [snapshot.id] * 3
