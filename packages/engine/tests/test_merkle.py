"""The Merkle tree: hashing, storage, diff; with property tests for the invariants."""

from __future__ import annotations

import hashlib
import random
import time

import msgpack
import pytest
import zstandard
from hypothesis import given, settings
from hypothesis import strategies as st

from sensei_engine.ingest.merkle import (
    ROOT,
    CorruptTreeError,
    Leaf,
    MerkleTree,
    build_tree,
    diff,
)


def _leaf(path: str, content: bytes) -> Leaf:
    return Leaf(path, hashlib.sha256(content).digest(), b"\1" * 16, len(content))


def _tree(files: dict[str, bytes]) -> MerkleTree:
    return build_tree(_leaf(path, content) for path, content in files.items())


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def test_hashes_follow_the_documented_encoding() -> None:
    tree = _tree({"README.md": b"hi", "src/app.py": b"x = 1"})
    src = _sha(b"f" + b"app.py" + b"\0" + _sha(b"x = 1"))
    root = _sha(b"f" + b"README.md" + b"\0" + _sha(b"hi") + b"d" + b"src" + b"\0" + src)
    assert tree.dirs == {ROOT: root, "src": src}
    assert tree.root == root


def test_children_sort_by_utf8_bytes() -> None:
    tree = _tree({"b": b"1", "a": b"2", "B": b"3", "é": b"4"})
    assert [entry.name for entry in tree.children[ROOT]] == ["B", "a", "b", "é"]


def test_an_empty_tree() -> None:
    tree = build_tree([])
    assert tree.root == _sha(b"")
    assert tree.files == {}


def test_names_matter_not_just_contents() -> None:
    assert _tree({"a.py": b"x"}).root != _tree({"b.py": b"x"}).root
    # A file and a directory of the same name are told apart by their kind.
    assert _tree({"x/y": b"1"}).root != _tree({"x": b"1"}).root


@pytest.mark.parametrize(
    "paths",
    [
        ["a.py", "a.py"],  # duplicate
        ["a", "a/b.py"],  # a file and a directory
        ["a/b.py", "a"],
        ["/abs.py"],
        ["a/../b.py"],
        ["./a.py"],
        ["a//b.py"],
        [""],
        ["a\0b.py"],
    ],
)
def test_invalid_trees_are_refused(paths: list[str]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the message varies with the case
        build_tree(_leaf(path, b"x") for path in paths)


def test_files_under() -> None:
    tree = _tree({"a/b/c.py": b"1", "a/d.py": b"2", "e.py": b"3"})
    assert sorted(leaf.path for leaf in tree.files_under("a")) == ["a/b/c.py", "a/d.py"]
    assert [leaf.path for leaf in tree.files_under("e.py")] == ["e.py"]
    assert len(list(tree.files_under(ROOT))) == 3
    assert list(tree.files_under("missing")) == []


# ------------------------------------------------------------------ storage


def test_storage_round_trip() -> None:
    tree = _tree({"README.md": b"hi", "src/app.py": b"x = 1", "src/lib/util.py": b""})
    loaded = MerkleTree.from_bytes(tree.to_bytes())
    assert loaded.root == tree.root
    assert loaded.dirs == tree.dirs
    assert loaded.files == tree.files


def _payload(tree: MerkleTree) -> dict[str, object]:
    raw = zstandard.ZstdDecompressor().decompress(tree.to_bytes())
    return msgpack.unpackb(raw, raw=False)  # type: ignore[no-any-return]


def _stored(payload: object) -> bytes:
    return zstandard.ZstdCompressor().compress(msgpack.packb(payload))  # type: ignore[arg-type]


def test_a_tampered_tree_is_refused() -> None:
    tree = _tree({"src/app.py": b"x = 1", "README.md": b"hi"})
    payload = _payload(tree)
    payload["dirs"]["src"][0][2] = _sha(b"something else")  # type: ignore[index]
    with pytest.raises(CorruptTreeError, match="don't add up"):
        MerkleTree.from_bytes(_stored(payload))


@pytest.mark.parametrize(
    "data",
    [
        b"not zstd at all",
        zstandard.ZstdCompressor().compress(b"\xc1"),  # not msgpack
        _stored({"v": 99, "root": b"", "dirs": {}}),
        _stored({"v": 1, "dirs": {"": [["a", "f"]]}}),  # rows missing fields
        _stored({"v": 1, "root": b"x", "dirs": {"": [["../x", "f", b"h", None, 1]]}}),
        _stored({"v": 1, "root": b"x", "dirs": {"": [["a", "f", b"h" * 32, None, "12"]]}}),
        _stored({"v": 1, "root": b"x", "dirs": {"": [["a", "f", b"h" * 32, None, -1]]}}),
        _stored({"v": 1, "root": b"x", "dirs": {"": [["a", "f", b"short", None, 1]]}}),
        _stored({"v": 1, "root": b"x", "dirs": {"": [["a", "f", b"h" * 32, b"xx", 1]]}}),
        _stored({"v": 1, "root": b"x", "dirs": {"": [["a", "x", b"h" * 32]]}}),
        _stored({"v": 1, "root": b"x", "dirs": ["not", "a", "dict"]}),
    ],
)
def test_garbage_is_refused(data: bytes) -> None:
    with pytest.raises(CorruptTreeError):
        MerkleTree.from_bytes(data)


# ------------------------------------------------------------------ diff


def test_diff_finds_each_kind_of_change() -> None:
    before = _tree(
        {
            "README.md": b"hi",
            "src/app.py": b"x = 1",
            "src/old.py": b"gone",
            "src/move_me.py": b"moving content",
            "docs/guide.md": b"same",
        }
    )
    after = _tree(
        {
            "README.md": b"hi",
            "src/app.py": b"x = 2",
            "lib/move_me.py": b"moving content",
            "src/new.py": b"fresh",
            "docs/guide.md": b"same",
        }
    )
    changes = diff(before, after)
    assert changes.added == ["src/new.py"]
    assert changes.modified == ["src/app.py"]
    assert changes.deleted == ["src/old.py"]
    assert changes.renamed == [("src/move_me.py", "lib/move_me.py")]
    assert changes.changed
    assert not diff(after, after).changed


def test_renames_prefer_the_same_file_name() -> None:
    before = _tree({"a/config.py": b"shared"})
    after = _tree({"b/other.py": b"shared", "c/config.py": b"shared"})
    changes = diff(before, after)
    assert changes.renamed == [("a/config.py", "c/config.py")]
    assert changes.added == ["b/other.py"]


def test_empty_files_are_never_renames() -> None:
    changes = diff(_tree({"a/__init__.py": b""}), _tree({"b/__init__.py": b""}))
    assert (changes.added, changes.deleted, changes.renamed) == (
        ["b/__init__.py"],
        ["a/__init__.py"],
        [],
    )


def test_names_pair_before_contents() -> None:
    before = _tree({"a/foo": b"shared", "b/LICENSE": b"shared"})
    after = _tree({"c/LICENSE": b"shared", "d/bar": b"shared"})
    assert diff(before, after).renamed == [("a/foo", "d/bar"), ("b/LICENSE", "c/LICENSE")]


def test_many_identical_files_diff_quickly() -> None:
    before = _tree({f"old/{i}.txt": b"same" for i in range(20_000)})
    after = _tree({f"new/n{i}.txt": b"same" for i in range(20_000)})
    started = time.monotonic()
    changes = diff(before, after)
    assert len(changes.renamed) == 20_000
    assert time.monotonic() - started < 5


def test_very_deep_paths() -> None:
    deep = "/".join(["d"] * 3000) + "/leaf.txt"
    tree = _tree({deep: b"x", "top.txt": b"y"})
    assert MerkleTree.from_bytes(tree.to_bytes()).root == tree.root
    assert [leaf.path for leaf in tree.files_under("d")] == [deep]
    assert diff(tree, _tree({deep: b"z", "top.txt": b"y"})).modified == [deep]


def test_a_file_replaced_by_a_directory() -> None:
    changes = diff(_tree({"x": b"file"}), _tree({"x/inner.py": b"dir now"}))
    assert (changes.added, changes.deleted, changes.modified) == (["x/inner.py"], ["x"], [])


# ------------------------------------------------------------------ properties

_names = st.sampled_from(["a", "b", "c", "src", "lib"])
_files = st.text(alphabet="xyz", min_size=1, max_size=3).map(lambda name: f"{name}.txt")
# Directories never end in ".txt", so no path is both a file and a directory.


def _join_path(dirs: list[str], name: str) -> str:
    return "/".join([*dirs, name])


_paths = st.builds(_join_path, st.lists(_names, max_size=3), _files)
_trees = st.dictionaries(_paths, st.binary(min_size=1, max_size=8), min_size=1, max_size=25)


def _ancestors(path: str) -> set[str]:
    parts = path.split("/")[:-1]
    return {ROOT} | {"/".join(parts[: i + 1]) for i in range(len(parts))}


@settings(max_examples=150, deadline=None)
@given(files=_trees, data=st.data())
def test_changing_a_file_changes_exactly_the_hashes_on_its_path(
    files: dict[str, bytes], data: st.DataObject
) -> None:
    path = data.draw(st.sampled_from(sorted(files)))
    changed = dict(files)
    changed[path] = files[path] + b"!"
    before, after = _tree(files), _tree(changed)
    assert before.dirs.keys() == after.dirs.keys()
    differing = {d for d in before.dirs if before.dirs[d] != after.dirs[d]}
    assert differing == _ancestors(path)
    assert diff(before, after).modified == [path]


@settings(max_examples=150, deadline=None)
@given(files=_trees, seed=st.integers())
def test_the_order_of_the_input_does_not_matter(files: dict[str, bytes], seed: int) -> None:
    leaves = [_leaf(path, content) for path, content in files.items()]
    shuffled = list(leaves)
    random.Random(seed).shuffle(shuffled)  # noqa: S311 - a test shuffle, not cryptography
    assert build_tree(leaves).dirs == build_tree(shuffled).dirs


@settings(max_examples=150, deadline=None)
@given(files=_trees, data=st.data())
def test_a_rename_is_detected(files: dict[str, bytes], data: st.DataObject) -> None:
    old = data.draw(st.sampled_from(sorted(files)))
    new = f"moved/{old}"
    content = b"unique:" + old.encode()  # no other file has this content
    before = _tree({**files, old: content})
    after_files = {p: c for p, c in files.items() if p != old}
    after = _tree({**after_files, new: content})
    changes = diff(before, after)
    assert changes.renamed == [(old, new)]
    assert (changes.added, changes.deleted, changes.modified) == ([], [], [])


@settings(max_examples=200, deadline=None)
@given(before=_trees, after=_trees)
def test_diff_agrees_with_comparing_the_file_lists(
    before: dict[str, bytes], after: dict[str, bytes]
) -> None:
    changes = diff(_tree(before), _tree(after))
    renamed_from = {old for old, _ in changes.renamed}
    renamed_to = {new for _, new in changes.renamed}
    assert set(changes.added) | renamed_to == after.keys() - before.keys()
    assert set(changes.deleted) | renamed_from == before.keys() - after.keys()
    assert set(changes.modified) == {
        p for p in before.keys() & after.keys() if before[p] != after[p]
    }
    for old, new in changes.renamed:
        assert before[old] == after[new]
    assert not set(changes.added) & renamed_to
    assert not set(changes.deleted) & renamed_from
