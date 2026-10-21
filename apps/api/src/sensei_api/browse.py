"""What browsing a snapshot needs in memory, loaded once and kept a while: its Merkle tree and
its constellation."""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from sensei_core.errors import NotFoundError
from sensei_core.models import Snapshot
from sensei_core.storage import Bucket, ObjectStorage, artifact_key
from sensei_engine.ingest.constellation import Constellation
from sensei_engine.ingest.merkle import ARTIFACT_NAME, MAX_STORED_BYTES, MerkleTree

# Trees are kept until they hold this many files between them (about 200 bytes each).
MAX_CACHED_FILES = 1_000_000
# Constellations: points between them (a point is about 300 bytes decoded).
MAX_CACHED_POINTS = 500_000


class TreeCache:
    """Merkle trees by snapshot, least recently used out first."""

    def __init__(self, *, max_files: int = MAX_CACHED_FILES) -> None:
        self._max_files = max_files
        self._trees: OrderedDict[uuid.UUID, MerkleTree] = OrderedDict()
        # One lock per snapshot being loaded: concurrent misses for it load it once, and
        # different snapshots load side by side.
        self._loading: dict[uuid.UUID, asyncio.Lock] = {}

    def _cached(self, snapshot: Snapshot) -> MerkleTree | None:
        tree = self._trees.get(snapshot.id)
        if tree is None or tree.root.hex() != snapshot.merkle_root:
            return None
        self._trees.move_to_end(snapshot.id)
        return tree

    async def get(self, storage: ObjectStorage, snapshot: Snapshot) -> MerkleTree:
        tree = self._cached(snapshot)
        if tree is not None:
            return tree
        lock = self._loading.setdefault(snapshot.id, asyncio.Lock())
        async with lock:
            tree = self._cached(snapshot)  # loaded while this one waited
            if tree is not None:
                return tree
            try:
                return await self._load(storage, snapshot)
            finally:
                self._loading.pop(snapshot.id, None)

    async def _load(self, storage: ObjectStorage, snapshot: Snapshot) -> MerkleTree:
        key = artifact_key(snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, ARTIFACT_NAME)
        data = await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=MAX_STORED_BYTES)
        if data is None:
            raise NotFoundError(
                "This snapshot can't be browsed (its tree is missing). Reindex it.",
                code="snapshot_not_browsable",
            )
        tree = MerkleTree.from_bytes(data)
        self._trees[snapshot.id] = tree
        self._trees.move_to_end(snapshot.id)
        while (
            len(self._trees) > 1
            and sum(len(t.files) for t in self._trees.values()) > self._max_files
        ):
            self._trees.popitem(last=False)
        return tree


class ConstellationCache:
    """Decoded constellations by (snapshot, generation), least recently used out first: a viewer
    reads one in pages, and each page shouldn't fetch and decode the whole thing again."""

    def __init__(self, *, max_points: int = MAX_CACHED_POINTS) -> None:
        self._max_points = max_points
        self._skies: OrderedDict[tuple[uuid.UUID, str], Constellation] = OrderedDict()
        self._loading: dict[tuple[uuid.UUID, str], asyncio.Lock] = {}

    async def get(
        self,
        snapshot_id: uuid.UUID,
        generation: str,
        load: Callable[[], Awaitable[Constellation]],
    ) -> Constellation:
        key = (snapshot_id, generation)
        found = self._cached(key)
        if found is not None:
            return found
        lock = self._loading.setdefault(key, asyncio.Lock())
        async with lock:
            found = self._cached(key)  # loaded while this one waited
            if found is not None:
                return found
            try:
                found = await load()
            finally:
                self._loading.pop(key, None)
            self._skies[key] = found
            self._skies.move_to_end(key)
            while (
                len(self._skies) > 1
                and sum(len(sky.points) for sky in self._skies.values()) > self._max_points
            ):
                self._skies.popitem(last=False)
            return found

    def _cached(self, key: tuple[uuid.UUID, str]) -> Constellation | None:
        found = self._skies.get(key)
        if found is not None:
            self._skies.move_to_end(key)
        return found
