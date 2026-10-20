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
from collections import defaultdict
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
        if path in self.files:
            yield self.files[path]
            return
        for entry in self.children.get(path, []):
            yield from self.files_under(_join(path, entry.name))

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
        dirs = cast("dict[str, list[list[Any]]]", payload["dirs"])
        for directory, rows in dirs.items():
            for row in rows:
                if row[1] == FILE:
                    name, _, content_hash, fast_hash, size = row
                    leaves.append(Leaf(_join(directory, name), content_hash, fast_hash, size))
        tree = build_tree(leaves)
        if tree.root != payload["root"] or set(tree.children) != set(dirs):
            raise CorruptTreeError("the hashes don't add up")
        return tree


def _check_path(path: str) -> None:
    if not path or path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/")):
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

    def visit(directory: str) -> bytes:
        entries: list[Entry] = []
        for name, kind in members[directory].items():
            path = _join(directory, name)
            child = files[path].content_hash if kind == FILE else visit(path)
            entries.append(Entry(name, kind, child))
        entries.sort(key=lambda e: e.name.encode())
        children[directory] = entries
        dirs[directory] = directory_hash(entries)
        return dirs[directory]

    visit(ROOT)
    return MerkleTree(files=files, dirs=dirs, children=children)


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
    (every empty file looks the same); within a content, same file names pair first."""
    by_hash: dict[bytes, list[Leaf]] = defaultdict(list)
    for leaf in added:
        if leaf.size > 0:
            by_hash[leaf.content_hash].append(leaf)
    renamed: list[tuple[str, str]] = []
    old_paths: set[str] = set()
    new_paths: set[str] = set()
    for old in sorted(deleted, key=lambda leaf: leaf.path):
        candidates = by_hash.get(old.content_hash)
        if old.size == 0 or not candidates:
            continue
        name = PurePosixPath(old.path).name
        match = next((c for c in candidates if PurePosixPath(c.path).name == name), candidates[0])
        candidates.remove(match)
        renamed.append((old.path, match.path))
        old_paths.add(old.path)
        new_paths.add(match.path)
    return renamed, old_paths, new_paths


def diff(before: MerkleTree, after: MerkleTree) -> TreeDiff:
    """What changed from ``before`` to ``after``; unchanged subtrees are skipped by hash."""
    added: list[Leaf] = []
    deleted: list[Leaf] = []
    modified: list[str] = []

    def compare(directory: str) -> None:
        old = {e.name: e for e in before.children.get(directory, [])}
        new = {e.name: e for e in after.children.get(directory, [])}
        for name in sorted(old.keys() | new.keys()):
            path = _join(directory, name)
            a, b = old.get(name), new.get(name)
            if a is not None and b is not None and a.kind == b.kind:
                if a.hash == b.hash:
                    continue
                if a.kind == DIR:
                    compare(path)
                else:
                    modified.append(path)
                continue
            if a is not None:
                deleted.extend(before.files_under(path))
            if b is not None:
                added.extend(after.files_under(path))

    if before.root != after.root:
        compare(ROOT)
    renamed, old_paths, new_paths = _pair_renames(deleted, added)
    return TreeDiff(
        added=sorted(leaf.path for leaf in added if leaf.path not in new_paths),
        modified=sorted(modified),
        deleted=sorted(leaf.path for leaf in deleted if leaf.path not in old_paths),
        renamed=sorted(renamed),
    )
