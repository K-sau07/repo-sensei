"""A Merkle tree over a snapshot's files, and the difference between two snapshots.

A file's hash is the SHA-256 of its content (``files.content_hash``). A directory's hash is the
SHA-256 over its children sorted by name, each as ``kind + name + NUL + hash`` (kind ``f`` or
``d``; names never contain NUL), much like a git tree. Equal hashes mean equal subtrees, so a diff
only walks the directories that changed. Only the files indexing keeps are in the tree, and a
directory exists only if it holds some of them.

Stored as zstd-compressed msgpack (``to_bytes``): for each directory, its children with their
hashes, and for files also the xxh3-128 hash (a fast way to check a file on disk against the
tree) and the size. Loading recomputes every hash and refuses a tree that doesn't add up.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, cast

import msgpack
import zstandard

FORMAT_VERSION = 1
ZSTD_LEVEL = 10
ARTIFACT_NAME = "merkle.msgpack.zst"  # stored per snapshot in the artifacts bucket
MAX_STORED_BYTES = 256 * 1024 * 1024
FILE, DIR = "f", "d"
ROOT = ""  # the root directory's path
HASH_BYTES = 32  # SHA-256

# msgpack ships no type information.
_pack = cast("Callable[[object], bytes]", msgpack.packb)  # pyright: ignore[reportUnknownMemberType]
_unpack = cast("Callable[..., object]", msgpack.unpackb)  # pyright: ignore[reportUnknownMemberType]


class CorruptTreeError(Exception):
    """A stored tree whose hashes don't add up (or that isn't a tree at all)."""


@dataclass(frozen=True, slots=True)
class Leaf:
    path: str  # relative, POSIX
    content_hash: bytes  # SHA-256
    fast_hash: bytes | None  # xxh3-128, when known
    size: int


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    kind: str  # FILE or DIR
    hash: bytes


def _parent(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return ROOT if parent == "." else parent


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def directory_hash(entries: Iterable[Entry]) -> bytes:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e.name.encode()):
        digest.update(entry.kind.encode() + entry.name.encode() + b"\0" + entry.hash)
    return digest.digest()


@dataclass(slots=True)
class MerkleTree:
    files: dict[str, Leaf]
    dirs: dict[str, bytes]  # directory path ("" for the root) -> hash
    children: dict[str, list[Entry]] = field(repr=False)  # sorted by name

    @property
    def root(self) -> bytes:
        return self.dirs[ROOT]

    def dir_hash(self, path: str) -> bytes | None:
        return self.dirs.get(path)

    def files_under(self, path: str) -> Iterator[Leaf]:
        """Every file at or below ``path`` (a file or a directory)."""
        pending = [path]
        while pending:  # no recursion: paths can be thousands of directories deep
            current = pending.pop()
            if current in self.files:
                yield self.files[current]
                continue
            entries = self.children.get(current, [])
            pending.extend(_join(current, entry.name) for entry in reversed(entries))

    # ------------------------------------------------------------------ storage

    def to_bytes(self) -> bytes:
        dirs: dict[str, list[list[Any]]] = {}
        for directory, entries in self.children.items():
            rows: list[list[Any]] = []
            for entry in entries:
                if entry.kind == FILE:
                    leaf = self.files[_join(directory, entry.name)]
                    rows.append([entry.name, FILE, entry.hash, leaf.fast_hash, leaf.size])
                else:
                    rows.append([entry.name, DIR, entry.hash])
            dirs[directory] = rows
        payload = {"v": FORMAT_VERSION, "root": self.root, "dirs": dirs}
        return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(_pack(payload))

    @classmethod
    def from_bytes(cls, data: bytes) -> MerkleTree:
        try:
            payload = _unpack(zstandard.ZstdDecompressor().decompress(data), raw=False)
            tree = cls._from_payload(cast("dict[str, Any]", payload))
        except CorruptTreeError:
            raise
        except Exception as exc:  # malformed in any way
            raise CorruptTreeError(str(exc)) from exc
        return tree

    @classmethod
    def _from_payload(cls, payload: dict[str, Any]) -> MerkleTree:
        if payload.get("v") != FORMAT_VERSION:
            raise CorruptTreeError(f"unknown format {payload.get('v')!r}")
        leaves: list[Leaf] = []
        dirs = payload["dirs"]
        if not isinstance(dirs, dict):
            raise CorruptTreeError("no directories")
        for directory, rows in cast("dict[object, object]", dirs).items():
            if not isinstance(directory, str) or not isinstance(rows, list):
                raise CorruptTreeError("a directory that isn't one")
            for row in cast("list[object]", rows):
                leaf = _stored_leaf(directory, row)
                if leaf is not None:
                    leaves.append(leaf)
        tree = build_tree(leaves)
        stored_dirs = set(cast("dict[str, object]", dirs))
        if tree.root != payload["root"] or set(tree.children) != stored_dirs:
            raise CorruptTreeError("the hashes don't add up")
        return tree


def _stored_leaf(directory: str, row: object) -> Leaf | None:
    """A file row of a stored tree (None for a directory row), checked field by field."""
    if not isinstance(row, list):
        raise CorruptTreeError("an entry that isn't a list")
    fields = cast("list[object]", row)
    if len(fields) == 3 and fields[1] == DIR:
        return None
    if len(fields) != 5 or fields[1] != FILE:
        raise CorruptTreeError("an entry of an unknown kind")
    name, _, content_hash, fast_hash, size = fields
    valid = (
        isinstance(name, str)
        and isinstance(content_hash, bytes)
        and len(content_hash) == HASH_BYTES
        and (fast_hash is None or (isinstance(fast_hash, bytes) and len(fast_hash) == 16))
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
    )
    if not valid:
        raise CorruptTreeError("a file entry with bad fields")
    return Leaf(
        _join(directory, cast("str", name)),
        cast("bytes", content_hash),
        cast("bytes | None", fast_hash),
        cast("int", size),
    )


def _check_path(path: str) -> None:
    if (
        not path
        or path.startswith("/")
        or "\0" in path  # names end at NUL in the encoding
        or any(p in ("", ".", "..") for p in path.split("/"))
    ):
        raise ValueError(f"not a relative, normalised path: {path!r}")


def build_tree(leaves: Iterable[Leaf]) -> MerkleTree:
    """The tree of ``leaves`` (in any order). Paths must be relative, normalised and unique, and
    no path may be both a file and a directory."""
    files: dict[str, Leaf] = {}
    members: dict[str, dict[str, str]] = defaultdict(dict)  # directory -> {name: kind}
    members[ROOT] = {}
    for leaf in leaves:
        _check_path(leaf.path)
        if leaf.path in files:
            raise ValueError(f"duplicate path {leaf.path!r}")
        files[leaf.path] = leaf
        path, kind = leaf.path, FILE
        while True:
            parent, name = _parent(path), PurePosixPath(path).name
            seen = members[parent].get(name)
            if seen is not None and seen != kind:
                raise ValueError(f"{path!r} is both a file and a directory")
            members[parent][name] = kind
            if parent == ROOT or seen == DIR:
                break  # the rest of the way up is recorded already
            path, kind = parent, DIR

    dirs: dict[str, bytes] = {}
    children: dict[str, list[Entry]] = {}
    # Deepest directories first, so every child's hash is known before its parent's (no
    # recursion: paths can be thousands of directories deep).
    for directory in sorted(members, key=_depth, reverse=True):
        entries: list[Entry] = []
        for name, kind in members[directory].items():
            path = _join(directory, name)
            child = files[path].content_hash if kind == FILE else dirs[path]
            entries.append(Entry(name, kind, child))
        entries.sort(key=lambda e: e.name.encode())
        children[directory] = entries
        dirs[directory] = directory_hash(entries)
    return MerkleTree(files=files, dirs=dirs, children=children)


def _depth(directory: str) -> int:
    return 0 if directory == ROOT else directory.count("/") + 1


# ------------------------------------------------------------------ diff


@dataclass(frozen=True, slots=True)
class TreeDiff:
    added: list[str]
    modified: list[str]
    deleted: list[str]
    renamed: list[tuple[str, str]]  # (old path, new path), same content

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.deleted or self.renamed)


def _pair_renames(
    deleted: list[Leaf], added: list[Leaf]
) -> tuple[list[tuple[str, str]], set[str], set[str]]:
    """Deleted and added files with identical content are renames. Empty files are never paired
    (every empty file looks the same). Same content and same file name pair first, then same
    content alone, each in path order. Linear: queues per key, no scanning."""
    by_name: dict[tuple[bytes, str], deque[str]] = defaultdict(deque)
    by_hash: dict[bytes, deque[str]] = defaultdict(deque)
    for leaf in sorted(added, key=lambda leaf: leaf.path):
        if leaf.size > 0:
            by_name[leaf.content_hash, PurePosixPath(leaf.path).name].append(leaf.path)
            by_hash[leaf.content_hash].append(leaf.path)
    renamed: list[tuple[str, str]] = []
    new_paths: set[str] = set()

    def take(queue: deque[str] | None) -> str | None:
        while queue:
            candidate = queue.popleft()
            if candidate not in new_paths:  # it may have paired through the other queue
                return candidate
        return None

    olds = sorted((leaf for leaf in deleted if leaf.size > 0), key=lambda leaf: leaf.path)
    unpaired: list[Leaf] = []
    for old in olds:
        match = take(by_name.get((old.content_hash, PurePosixPath(old.path).name)))
        if match is None:
            unpaired.append(old)
        else:
            renamed.append((old.path, match))
            new_paths.add(match)
    for old in unpaired:
        match = take(by_hash.get(old.content_hash))
        if match is not None:
            renamed.append((old.path, match))
            new_paths.add(match)
    return renamed, {old for old, _ in renamed}, new_paths


def diff(before: MerkleTree, after: MerkleTree) -> TreeDiff:
    """What changed from ``before`` to ``after``; unchanged subtrees are skipped by hash."""
    added: list[Leaf] = []
    deleted: list[Leaf] = []
    modified: list[str] = []

    pending = [ROOT] if before.root != after.root else []
    while pending:  # directories whose hashes differ
        directory = pending.pop()
        old = {e.name: e for e in before.children.get(directory, [])}
        new = {e.name: e for e in after.children.get(directory, [])}
        for name in sorted(old.keys() | new.keys()):
            path = _join(directory, name)
            a, b = old.get(name), new.get(name)
            if a is not None and b is not None and a.kind == b.kind:
                if a.hash == b.hash:
                    continue
                if a.kind == DIR:
                    pending.append(path)
                else:
                    modified.append(path)
                continue
            if a is not None:
                deleted.extend(before.files_under(path))
            if b is not None:
                added.extend(after.files_under(path))
    renamed, old_paths, new_paths = _pair_renames(deleted, added)
    return TreeDiff(
        added=sorted(leaf.path for leaf in added if leaf.path not in new_paths),
        modified=sorted(modified),
        deleted=sorted(leaf.path for leaf in deleted if leaf.path not in old_paths),
        renamed=sorted(renamed),
    )
