"""The Living Constellation: where each file of a snapshot sits in 3D, and how that is stored.

Layout v1 is a *directory layout*, a treemap on nested spheres. Every directory owns a patch of
the sphere and shares it out among its own files and its subdirectories in proportion to the
files each holds (a squarified treemap over an equal-area map of the sphere, so a patch's area is
its share of the files). A directory's files sit inside their part of the patch, on the shell for
the directory's depth; shells grow outward with depth, the root's files nearest the centre. So
clusters are directories and subdirectories fan out behind their parents. Nothing is random: the
same paths always give the same picture (within a cluster, files follow a low-discrepancy
sequence in name order, and a hash of the path nudges each one off its shell a little).
Positions fall inside the unit sphere. The semantic layout (by embeddings) comes later.

Stored per snapshot as zstd-compressed msgpack (:meth:`Constellation.to_bytes`): the directories
(path, parent, depth, anchor, direct file count) and the points in discovery order (path order),
coordinates quantised to 1e-4. A point's index in that order is its cursor.

While discovery runs, the points also go to Redis as they are found (:class:`LiveConstellation`):
a header with the directories under ``constellation:{job}:head`` and fixed-size batches of points
under ``constellation:{job}:points``, so the API can page through what is there so far.
"""

from __future__ import annotations

import base64
import hashlib
import math
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast

import msgpack
import orjson
import zstandard
from redis.asyncio import Redis

from sensei_engine.ingest.roles import Role

FORMAT_VERSION = 1
LAYOUT = "directory"
ZSTD_LEVEL = 10
ARTIFACT_NAME = "constellation.bin"  # stored per snapshot in the artifacts bucket
MAX_STORED_BYTES = 128 * 1024 * 1024
ROOT = ""  # the root directory's path
QUANTUM = 10_000  # stored coordinates are integers: round(x * QUANTUM)

SHELL_BASE = 0.75  # the shell for depth d has radius 1 - SHELL_BASE ** (d + 1)
SHELL_JITTER = 0.02  # how far a file may sit off its shell
PATCH_INSET = 0.06  # each side of a child patch gives up this share, leaving gaps between them
CLUSTER_INSET = 0.1  # and the files keep this far from their patch's edges
_PLASTIC = 1.324717957244746  # R2 sequence (Roberts): evenly spread points in a square
_R2 = (1 / _PLASTIC, 1 / _PLASTIC**2)

LIVE_BATCH = 500  # points per Redis entry
LIVE_TTL_SECONDS = 6 * 3600  # while discovery runs (refreshed by every batch)
LIVE_KEEP_SECONDS = 3600  # after it is done (the stored artifact answers from then on)

# Which layer each role is drawn in (the design system's four, and the rest).
LAYERS: dict[str, str] = {
    Role.UI.value: "ui",
    Role.API.value: "api",
    Role.SERVICE.value: "svc",
    Role.DB.value: "db",
}
OTHER_LAYER = "other"

# msgpack ships no type information.
_pack = cast("Callable[[object], bytes]", msgpack.packb)  # pyright: ignore[reportUnknownMemberType]
_unpack = cast("Callable[..., object]", msgpack.unpackb)  # pyright: ignore[reportUnknownMemberType]

Position = tuple[float, float, float]


class CorruptConstellationError(Exception):
    """A stored constellation that can't be read."""


def layer_for(role: str) -> str:
    return LAYERS.get(role, OTHER_LAYER)


@dataclass(frozen=True, slots=True)
class Directory:
    path: str  # "" for the root
    parent: int | None  # index in the directory list (None for the root)
    depth: int  # the root is 0
    position: Position  # its anchor: where links to it meet
    files: int  # files directly inside


@dataclass(frozen=True, slots=True)
class Point:
    file_id: uuid.UUID
    path: str
    position: Position
    role: str
    language: str | None
    directory: int  # index in the directory list
    size: int  # bytes
    loc: int | None

    @property
    def layer(self) -> str:
        return layer_for(self.role)


# ------------------------------------------------------------------ layout


@dataclass(frozen=True, slots=True)
class Patch:
    """A piece of the sphere: azimuth u0..u1 (fractions of a turn) by height v0..v1 (-1..1).
    Equal areas in (u, v) are equal areas on the sphere."""

    u0: float
    u1: float
    v0: float
    v1: float

    def inset(self, share: float) -> Patch:
        du, dv = (self.u1 - self.u0) * share, (self.v1 - self.v0) * share
        return Patch(self.u0 + du, self.u1 - du, self.v0 + dv, self.v1 - dv)

    @property
    def middle(self) -> tuple[float, float]:
        return (self.u0 + self.u1) / 2, (self.v0 + self.v1) / 2


def shell(depth: int) -> float:
    return 1 - SHELL_BASE ** (depth + 1)


def _on_sphere(u: float, v: float, radius: float) -> Position:
    theta = 2 * math.pi * u
    ring = math.sqrt(max(0.0, 1 - v * v))
    return (
        _round(radius * ring * math.cos(theta)),
        _round(radius * v),
        _round(radius * ring * math.sin(theta)),
    )


def _round(value: float) -> float:
    return round(value * QUANTUM) / QUANTUM


def _worst(row_sum: float, largest: float, smallest: float, side: float) -> float:
    """The worst aspect ratio in a row of rectangles laid along ``side``."""
    squared = side * side
    return max(squared * largest / row_sum**2, row_sum**2 / (squared * smallest))


def squarify(sizes: Sequence[float], width: float, height: float) -> list[Patch]:
    """Rectangles (as x0, x1, y0, y1 in a ``width`` by ``height`` box) with areas proportional to
    ``sizes`` (sorted largest first, none zero), as close to square as the squarified treemap
    gets them (Bruls, Huizing and van Wijk). Rows are laid along the box's shorter side."""
    if width <= 0 or height <= 0:  # nothing to share: everything gets the (empty) box
        return [Patch(0.0, width, 0.0, height) for _ in sizes]
    total = sum(sizes)
    areas = [size * width * height / total for size in sizes]
    out: list[Patch] = []
    x, y, w, h = 0.0, 0.0, width, height
    start = 0
    while start < len(areas):
        side = max(min(w, h), 1e-12)  # rounding can leave the last row a sliver
        end, row_sum = start + 1, areas[start]
        ratio = _worst(row_sum, areas[start], areas[start], side)
        while end < len(areas):
            candidate = _worst(row_sum + areas[end], areas[start], areas[end], side)
            if candidate > ratio:
                break
            ratio, row_sum, end = candidate, row_sum + areas[end], end + 1
        thickness = row_sum / side
        offset = 0.0
        for area in areas[start:end]:
            length = area / thickness
            if w >= h:  # a column down the left
                out.append(Patch(x, x + thickness, y + offset, y + offset + length))
            else:  # a row across the top
                out.append(Patch(x + offset, x + offset + length, y, y + thickness))
            offset += length
        if w >= h:
            x, w = x + thickness, max(0.0, w - thickness)
        else:
            y, h = y + thickness, max(0.0, h - thickness)
        start = end
    return out


def _split(patch: Patch, weights: Sequence[int]) -> list[Patch]:
    """``patch`` shared out in proportion to ``weights`` (in that order; none zero)."""
    if len(weights) == 1:
        return [patch]
    # Measured as it looks on the sphere around the patch's middle height: a turn is 2π·ring
    # wide there and a unit of height is 1/ring tall (the map stretches heights near the poles).
    ring = max(0.1, math.sqrt(max(0.0, 1 - patch.middle[1] ** 2)))
    width = (patch.u1 - patch.u0) * 2 * math.pi * ring
    height = (patch.v1 - patch.v0) / ring
    order = sorted(range(len(weights)), key=lambda i: (-weights[i], i))
    rects = squarify([weights[i] for i in order], width, height)
    out: list[Patch | None] = [None] * len(weights)
    du, dv = patch.u1 - patch.u0, patch.v1 - patch.v0
    for index, rect in zip(order, rects, strict=True):
        out[index] = Patch(
            patch.u0 + du * rect.u0 / width if width else patch.u0,
            patch.u0 + du * rect.u1 / width if width else patch.u1,
            patch.v0 + dv * rect.v0 / height if height else patch.v0,
            patch.v0 + dv * rect.v1 / height if height else patch.v1,
        )
    return [p for p in out if p is not None]


def _parent(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return ROOT if parent == "." else parent


def _nudge(path: str) -> float:
    """-1..1, from the path alone."""
    digest = hashlib.blake2b(path.encode(), digest_size=8).digest()
    return int.from_bytes(digest) / 2**63 - 1


class DirectoryLayout:
    """The directory layout of a fixed set of file paths (relative, normalised, unique)."""

    def __init__(self, paths: Iterable[str]) -> None:
        files: dict[str, list[str]] = defaultdict(list)
        subdirs: dict[str, set[str]] = defaultdict(set)
        known = {ROOT}
        for path in paths:
            directory = _parent(path)
            files[directory].append(path)
            while directory not in known:  # record the way up, once
                known.add(directory)
                parent = _parent(directory)
                subdirs[parent].add(directory)
                directory = parent
        weight: dict[str, int] = {}
        # Deepest first, so every subdirectory's count is known before its parent's (no
        # recursion: paths can be thousands of directories deep).
        for directory in sorted(known, key=_depth, reverse=True):
            weight[directory] = len(files.get(directory, ())) + sum(
                weight[child] for child in subdirs.get(directory, ())
            )

        self.dirs: list[Directory] = []
        self._index: dict[str, int] = {}
        self._spots: dict[str, Position] = {}
        pending: list[tuple[str, Patch, int | None]] = [(ROOT, Patch(0.0, 1.0, -1.0, 1.0), None)]
        while pending:  # parents before children, so a parent's index is always known
            directory, patch, parent = pending.pop()
            depth = _depth(directory)
            own = sorted(files.get(directory, ()))
            children = sorted(subdirs.get(directory, ()))
            weights = ([len(own)] if own else []) + [weight[child] for child in children]
            parts = _split(patch, weights) if weights else []
            if own:
                cluster, parts = parts[0], parts[1:]
                self._place(own, cluster.inset(CLUSTER_INSET), depth)
                u, v = cluster.middle
            else:
                u, v = patch.middle
            anchor = (0.0, 0.0, 0.0) if directory == ROOT else _on_sphere(u, v, shell(depth))
            index = len(self.dirs)
            self._index[directory] = index
            self.dirs.append(Directory(directory, parent, depth, anchor, len(own)))
            # Reversed onto the stack: children come out (and are numbered) in name order.
            for child, part in reversed(list(zip(children, parts, strict=True))):
                pending.append((child, part.inset(PATCH_INSET), index))

    def _place(self, paths: list[str], patch: Patch, depth: int) -> None:
        radius = shell(depth)
        for i, path in enumerate(paths):
            a = (0.5 + i * _R2[0]) % 1
            b = (0.5 + i * _R2[1]) % 1
            u = patch.u0 + a * (patch.u1 - patch.u0)
            v = patch.v0 + b * (patch.v1 - patch.v0)
            self._spots[path] = _on_sphere(u, v, radius + SHELL_JITTER * _nudge(path))

    def __len__(self) -> int:
        return len(self._spots)

    def point(
        self,
        *,
        file_id: uuid.UUID,
        path: str,
        role: str,
        language: str | None,
        size: int,
        loc: int | None,
    ) -> Point:
        """The point for one of the layout's files (``KeyError`` for any other path)."""
        return Point(
            file_id=file_id,
            path=path,
            position=self._spots[path],
            role=role,
            language=language,
            directory=self._index[_parent(path)],
            size=size,
            loc=loc,
        )


def _depth(directory: str) -> int:
    return 0 if directory == ROOT else directory.count("/") + 1


# ------------------------------------------------------------------ storage


def _q(value: float) -> int:
    return round(value * QUANTUM)


def _dir_row(directory: Directory) -> list[Any]:
    x, y, z = directory.position
    parent = -1 if directory.parent is None else directory.parent
    return [directory.path, parent, directory.depth, _q(x), _q(y), _q(z), directory.files]


def _point_row(point: Point) -> list[Any]:
    x, y, z = point.position
    return [
        point.file_id.bytes,
        point.path,
        _q(x),
        _q(y),
        _q(z),
        point.role,
        point.language,
        point.directory,
        point.size,
        point.loc,
    ]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _dir_from_row(row: object) -> Directory:
    if not isinstance(row, list) or len(cast("list[object]", row)) != 7:
        raise CorruptConstellationError("a directory entry that isn't one")
    path, parent, depth, x, y, z, files = cast("list[object]", row)
    if not (isinstance(path, str) and all(_is_int(v) for v in (parent, depth, x, y, z, files))):
        raise CorruptConstellationError("a directory entry with bad fields")
    parent_index = cast("int", parent)
    return Directory(
        path,
        None if parent_index < 0 else parent_index,
        cast("int", depth),
        (cast("int", x) / QUANTUM, cast("int", y) / QUANTUM, cast("int", z) / QUANTUM),
        cast("int", files),
    )


def _point_from_row(row: object) -> Point:
    if not isinstance(row, list) or len(cast("list[object]", row)) != 10:
        raise CorruptConstellationError("a point entry that isn't one")
    file_id, path, x, y, z, role, language, directory, size, loc = cast("list[object]", row)
    valid = (
        isinstance(file_id, bytes)
        and len(file_id) == 16
        and isinstance(path, str)
        and all(_is_int(v) for v in (x, y, z, directory, size))
        and isinstance(role, str)
        and (language is None or isinstance(language, str))
        and (loc is None or _is_int(loc))
    )
    if not valid:
        raise CorruptConstellationError("a point entry with bad fields")
    return Point(
        file_id=uuid.UUID(bytes=cast("bytes", file_id)),
        path=cast("str", path),
        position=(cast("int", x) / QUANTUM, cast("int", y) / QUANTUM, cast("int", z) / QUANTUM),
        role=cast("str", role),
        language=cast("str | None", language),
        directory=cast("int", directory),
        size=cast("int", size),
        loc=cast("int | None", loc),
    )


@dataclass(slots=True)
class Constellation:
    """A snapshot's whole constellation. ``generation`` names the discovery that made it (a
    viewer holding points of another generation starts over)."""

    generation: str
    dirs: list[Directory]
    points: list[Point]

    @classmethod
    def build(
        cls, generation: str, layout: DirectoryLayout, points: Iterable[Point]
    ) -> Constellation:
        return cls(generation, list(layout.dirs), list(points))

    def to_bytes(self) -> bytes:
        payload = {
            "v": FORMAT_VERSION,
            "layout": LAYOUT,
            "generation": self.generation,
            "dirs": [_dir_row(d) for d in self.dirs],
            "points": [_point_row(p) for p in self.points],
        }
        return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(_pack(payload))

    @classmethod
    def from_bytes(cls, data: bytes) -> Constellation:
        try:
            raw = _unpack(zstandard.ZstdDecompressor().decompress(data), raw=False)
            if not isinstance(raw, dict):
                raise CorruptConstellationError("not a constellation")
            payload = cast("dict[str, object]", raw)
            if payload.get("v") != FORMAT_VERSION or payload.get("layout") != LAYOUT:
                raise CorruptConstellationError(f"unknown format {payload.get('v')!r}")
            generation, dirs, points = payload["generation"], payload["dirs"], payload["points"]
            if not isinstance(generation, str) or not isinstance(dirs, list):
                raise CorruptConstellationError("no directories")
            if not isinstance(points, list):
                raise CorruptConstellationError("no points")
            found = cls(
                generation,
                [_dir_from_row(row) for row in cast("list[object]", dirs)],
                [_point_from_row(row) for row in cast("list[object]", points)],
            )
        except CorruptConstellationError:
            raise
        except Exception as exc:  # malformed in any way
            raise CorruptConstellationError(str(exc)) from exc
        if any(not 0 <= p.directory < len(found.dirs) for p in found.points):
            raise CorruptConstellationError("a point in no directory")
        return found


# ------------------------------------------------------------------ while discovery runs


def live_keys(job_id: uuid.UUID | str) -> tuple[str, str]:
    """The header and the point batches of a run's constellation in progress."""
    return f"constellation:{job_id}:head", f"constellation:{job_id}:points"


def _seal(value: object) -> str:
    # The shared Redis client decodes replies as text, so compressed bytes travel as base64.
    return base64.b64encode(zstandard.ZstdCompressor().compress(orjson.dumps(value))).decode()


def _open(value: str) -> Any:
    return orjson.loads(zstandard.ZstdDecompressor().decompress(base64.b64decode(value)))


def _point_json(point: Point) -> list[Any]:
    row = _point_row(point)
    row[0] = point.file_id.hex
    return row


def _point_from_json(row: list[Any]) -> Point:
    return _point_from_row([uuid.UUID(hex=row[0]).bytes, *row[1:]])


class LiveConstellation:
    """Writes a run's points to Redis as discovery finds them: :meth:`begin` once the walk has
    listed every file, :meth:`add` as files are read (full batches go out as they fill),
    :meth:`finish` at the end. ``add`` and ``finish`` return the cursor: how many points can be
    read."""

    def __init__(
        self, redis: Redis, job_id: uuid.UUID, layout: DirectoryLayout, generation: str
    ) -> None:
        self._redis = redis
        self._head, self._points = live_keys(job_id)
        self.layout = layout
        self.generation = generation
        self._pending: list[Point] = []
        self.cursor = 0

    @property
    def total(self) -> int:
        return len(self.layout)

    async def begin(self) -> None:
        header = {
            "v": FORMAT_VERSION,
            "layout": LAYOUT,
            "generation": self.generation,
            "total": self.total,
            "batch": LIVE_BATCH,
            "dirs": [_dir_row(d) for d in self.layout.dirs],
        }
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._head, self._points)  # an earlier attempt's
            pipe.set(self._head, _seal(header), ex=LIVE_TTL_SECONDS)
            await cast("Awaitable[object]", pipe.execute())  # pyright: ignore[reportUnknownMemberType]

    async def add(self, points: Iterable[Point]) -> int:
        self._pending.extend(points)
        batches: list[list[Point]] = []
        while len(self._pending) >= LIVE_BATCH:
            batches.append(self._pending[:LIVE_BATCH])
            del self._pending[:LIVE_BATCH]
        await self._push(batches, LIVE_TTL_SECONDS)
        return self.cursor

    async def finish(self) -> int:
        """Send what is left (the only batch that may be short) and keep it all a while."""
        batches = [self._pending] if self._pending else []
        self._pending = []
        await self._push(batches, LIVE_TTL_SECONDS)
        return self.cursor

    async def done(self) -> None:
        """The stored artifact has it all now: let the live copy expire soon."""
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.expire(self._head, LIVE_KEEP_SECONDS)
            pipe.expire(self._points, LIVE_KEEP_SECONDS)
            await cast("Awaitable[object]", pipe.execute())  # pyright: ignore[reportUnknownMemberType]

    async def _push(self, batches: list[list[Point]], ttl: int) -> None:
        if not batches:
            return
        async with self._redis.pipeline(transaction=True) as pipe:
            for batch in batches:
                pipe.rpush(self._points, _seal([_point_json(p) for p in batch]))
            pipe.expire(self._head, ttl)
            pipe.expire(self._points, ttl)
            await cast("Awaitable[object]", pipe.execute())  # pyright: ignore[reportUnknownMemberType]
        self.cursor += sum(len(batch) for batch in batches)


@dataclass(frozen=True, slots=True)
class LivePage:
    generation: str
    total: int  # points the run will have
    available: int  # points there are so far
    dirs: list[Directory]
    points: list[Point]  # from the cursor asked for, at most the limit


async def read_live(redis: Redis, job_id: uuid.UUID, since: int, limit: int) -> LivePage | None:
    """Points ``since`` (a cursor) onward of a run's constellation in progress, at most
    ``limit``; None if the run has none (its walk hasn't finished, or it expired)."""
    head, points = live_keys(job_id)
    first = since // LIVE_BATCH
    last = (since + limit - 1) // LIVE_BATCH
    async with redis.pipeline(transaction=True) as pipe:  # one consistent view
        pipe.get(head)
        pipe.llen(points)
        pipe.lrange(points, first, last)  # pyright: ignore[reportUnknownMemberType]
        sealed, length, batches = cast(
            "list[Any]",
            await cast("Awaitable[object]", pipe.execute()),  # pyright: ignore[reportUnknownMemberType]
        )
    if sealed is None:
        return None
    try:
        header = cast("dict[str, Any]", _open(cast("str", sealed)))
        if header.get("v") != FORMAT_VERSION or header.get("batch") != LIVE_BATCH:
            return None  # written by another version: as good as missing
        total = int(header["total"])
        rows = [row for batch in cast("list[str]", batches) for row in _open(batch)]
        found = [_point_from_json(row) for row in rows]
        dirs = [_dir_from_row(row) for row in header["dirs"]] if since == 0 else []
    except (CorruptConstellationError, ValueError, KeyError, TypeError, zstandard.ZstdError):
        return None
    offset = since - first * LIVE_BATCH
    return LivePage(
        generation=str(header["generation"]),
        total=total,
        # Every batch but the last one is full.
        available=min(total, int(length) * LIVE_BATCH),
        dirs=dirs,
        points=found[offset : offset + limit],
    )
