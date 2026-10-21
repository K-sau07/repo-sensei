"""Unpacked snapshots: safe paths, reads that never follow links, and the cache."""

from __future__ import annotations

import asyncio
import io
import tarfile
import uuid
from pathlib import Path

import pytest
import zstandard

from sensei_engine.snapshots import (
    READY_MARKER,
    SnapshotCache,
    UnsafePathError,
    check_path,
    open_file,
    read_file,
)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [("", ""), ("/", ""), ("src", "src"), ("src/", "src"), ("a/b.py", "a/b.py")],
)
def test_paths_that_are_fine(raw: str, clean: str) -> None:
    assert check_path(raw) == clean


@pytest.mark.parametrize(
    "raw",
    ["/etc/passwd", "../etc/passwd", "a/../../b", "a/./b", "a//b", "..", "a\\b", "a\0b"],
)
def test_paths_that_are_refused(raw: str) -> None:
    with pytest.raises(UnsafePathError):
        check_path(raw)


def test_reads_never_follow_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s3cret")
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "src" / "link.txt").symlink_to(outside / "secret.txt")
    assert read_file(root, "src/app.py", max_bytes=100) == b"x = 1\n"
    for path in ("linked/secret.txt", "src/link.txt"):
        with pytest.raises(OSError):  # noqa: PT011 - ELOOP or ENOTDIR depending on the part
            read_file(root, path, max_bytes=100)
    with pytest.raises(ValueError, match="larger than"):
        read_file(root, "src/app.py", max_bytes=3)
    # Only regular files open: a directory isn't one.
    with pytest.raises(FileNotFoundError):
        open_file(root, "src")
    with open_file(root, "src/app.py") as handle:
        assert handle.read() == b"x = 1\n"


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor().compress(buffer.getvalue())


async def test_the_cache_unpacks_once_and_evicts_the_least_recent(tmp_path: Path) -> None:
    cache = SnapshotCache(tmp_path / "cache", max_bytes=1500, max_snapshot_bytes=10_000)
    downloads: list[str] = []

    def source(name: str, payload: bytes):
        async def download(out: Path) -> None:
            downloads.append(name)
            await asyncio.to_thread(out.write_bytes, _archive({"f.txt": payload}))

        return download

    first, second = uuid.uuid4(), uuid.uuid4()
    one = await cache.unpacked(first, source("one", b"a" * 1000))
    assert (one / "f.txt").read_bytes() == b"a" * 1000
    assert (one / READY_MARKER).is_file()
    assert await cache.unpacked(first, source("one", b"")) == one  # no second download
    await cache.unpacked(second, source("two", b"b" * 1000))  # over budget: the first goes
    assert downloads == ["one", "two"]
    assert not one.exists()
    assert not list((tmp_path / "cache").glob(".unpack-*"))
