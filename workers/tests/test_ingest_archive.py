"""The snapshot archive and the working-copy cache."""

from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path

import pytest
import zstandard

from sensei_workers.ingest.archive import ArchiveTooLargeError, extract_archive, write_archive
from sensei_workers.ingest.cache import CacheBusyError, evict, repo_lock
from sensei_workers.ingest.git import LAST_USED_MARKER


def _worktree(root: Path) -> Path:
    tree = root / "tree"
    (tree / ".git" / "objects").mkdir(parents=True)
    (tree / ".git" / "config").write_text("[core]\n")
    (tree / "src").mkdir()
    (tree / "src" / "b.py").write_text("b = 2\n")
    (tree / "src" / "a.py").write_text("a = 1\n")
    script = tree / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o750)
    (tree / "outside").symlink_to("../../etc/passwd")
    (tree / "linked-dir").symlink_to("src")
    os.mkfifo(tree / "pipe")
    return tree


def _members(path: Path) -> list[tarfile.TarInfo]:
    with path.open("rb") as compressed:
        raw = zstandard.ZstdDecompressor().stream_reader(compressed).read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        return tar.getmembers()


def test_archive_contents(tmp_path: Path) -> None:
    tree = _worktree(tmp_path)
    info = write_archive(tree, tmp_path / "snap.tar.zst")
    members = _members(info.path)
    names = [m.name for m in members]
    assert names == ["linked-dir", "outside", "run.sh", "src", "src/a.py", "src/b.py"]
    assert info.entries == len(names)
    assert not any(name.startswith(".git") for name in names)  # no repository internals
    assert "pipe" not in names  # only files, directories and links
    by_name = {m.name: m for m in members}
    # Links are stored as links, never followed (nothing from /etc gets in).
    assert by_name["outside"].issym()
    assert by_name["outside"].linkname == "../../etc/passwd"
    assert by_name["linked-dir"].issym()
    assert by_name["run.sh"].mode == 0o755
    assert by_name["src/a.py"].mode == 0o644
    assert {(m.uid, m.gid, m.uname, m.mtime) for m in members} == {(0, 0, "", 0)}


def test_the_same_tree_gives_the_same_bytes(tmp_path: Path) -> None:
    tree = _worktree(tmp_path)
    first = write_archive(tree, tmp_path / "one.tar.zst").path.read_bytes()
    time.sleep(0.01)
    (tree / "src" / "a.py").touch()  # a newer mtime alone changes nothing
    second = write_archive(tree, tmp_path / "two.tar.zst").path.read_bytes()
    assert first == second


def test_unpacking_restores_the_tree(tmp_path: Path) -> None:
    info = write_archive(_worktree(tmp_path), tmp_path / "snap.tar.zst")
    dest = tmp_path / "unpacked"
    written = extract_archive(info.path, dest, max_bytes=1024)
    assert (dest / "src" / "a.py").read_text() == "a = 1\n"
    assert os.access(dest / "run.sh", os.X_OK)
    assert (dest / "linked-dir").is_symlink()  # a link inside the tree is fine
    assert not (dest / "outside").exists()  # one pointing out of it is skipped
    assert not (dest / "outside").is_symlink()
    assert written == info.entries - 1


def _hostile(path: Path) -> Path:
    def add(tar: tarfile.TarFile, name: str, data: bytes = b"x", **fields: object) -> None:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        for key, value in fields.items():
            setattr(member, key, value)
        tar.addfile(member, io.BytesIO(data))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        add(tar, "ok.txt")
        add(tar, "../escape.txt")
        add(tar, "/absolute.txt")
        add(tar, "etc", b"", type=tarfile.SYMTYPE, linkname="/etc")
        add(tar, "etc/hosts.txt")
        add(tar, "dev", b"", type=tarfile.CHRTYPE)
    path.write_bytes(zstandard.ZstdCompressor().compress(buffer.getvalue()))
    return path


def test_unpacking_never_writes_outside_the_destination(tmp_path: Path) -> None:
    dest = tmp_path / "inner" / "dest"
    extract_archive(_hostile(tmp_path / "evil.tar.zst"), dest, max_bytes=1024)
    unpacked = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*"))
    # "/absolute.txt" loses its leading slash; the link to /etc is refused, so "etc/hosts.txt"
    # becomes a plain directory and file inside dest; the escape and the device are dropped.
    assert unpacked == ["absolute.txt", "etc", "etc/hosts.txt", "ok.txt"]
    assert not (dest / "etc").is_symlink()
    assert not (tmp_path / "inner" / "escape.txt").exists()


def test_unpacking_stops_past_the_size_limit(tmp_path: Path) -> None:
    info = write_archive(_worktree(tmp_path), tmp_path / "snap.tar.zst")
    with pytest.raises(ArchiveTooLargeError):
        extract_archive(info.path, tmp_path / "unpacked", max_bytes=10)


# ------------------------------------------------------------------ cache


def _copy(cache: Path, name: str, size: int, last_used: float) -> Path:
    copy = cache / name
    (copy / ".git").mkdir(parents=True)
    (copy / "data.bin").write_bytes(b"x" * size)
    (copy / ".git" / LAST_USED_MARKER).write_text(str(last_used))
    return copy


def test_eviction_removes_the_least_recently_used(tmp_path: Path) -> None:
    now = 1_000_000.0
    oldest = _copy(tmp_path, "oldest", 400, now - 30_000)
    older = _copy(tmp_path, "older", 400, now - 20_000)
    recent = _copy(tmp_path, "recent", 400, now - 10_000)
    removed = evict(tmp_path, max_bytes=1000, keep=set(), now=now)
    # About 1230 bytes (with the markers) over a 1000 budget: oldest first, until at most 900.
    assert removed == [oldest]
    assert older.exists()
    assert recent.exists()
    assert evict(tmp_path, max_bytes=500, keep=set(), now=now) == [older]


def test_eviction_spares_active_and_recent_copies(tmp_path: Path) -> None:
    now = 1_000_000.0
    indexing = _copy(tmp_path, "indexing", 400, now - 50_000)
    just_used = _copy(tmp_path, "just-used", 400, now - 60)
    idle = _copy(tmp_path, "idle", 400, now - 40_000)
    removed = evict(tmp_path, max_bytes=500, keep={"indexing"}, now=now)
    assert removed == [idle]
    assert indexing.exists()
    assert just_used.exists()


def test_nothing_happens_within_budget(tmp_path: Path) -> None:
    _copy(tmp_path, "a", 100, 0)
    assert evict(tmp_path, max_bytes=1000, keep=set(), now=1e9) == []
    assert evict(tmp_path / "missing", max_bytes=1, keep=set()) == []


def test_a_copy_in_use_is_never_evicted(tmp_path: Path) -> None:
    now = 1_000_000.0
    busy = _copy(tmp_path, "busy", 400, now - 50_000)
    idle = _copy(tmp_path, "idle", 400, now - 40_000)
    with repo_lock(tmp_path, "busy"):
        with pytest.raises(CacheBusyError), repo_lock(tmp_path, "busy", wait=False):
            pass
        assert evict(tmp_path, max_bytes=100, keep=set(), now=now) == [idle]
    assert busy.exists()


def test_bookkeeping_is_not_a_copy_and_stale_scratch_goes(tmp_path: Path) -> None:
    now = time.time()
    scratch = tmp_path / ".unpack-abc"
    scratch.mkdir()
    (tmp_path / ".archives").mkdir()
    partial = tmp_path / ".archives" / "job.tar.zst"
    partial.write_bytes(b"x" * 5000)
    fresh = tmp_path / ".archives" / "now.tar.zst"
    fresh.write_bytes(b"x")
    for old in (scratch, partial):
        os.utime(old, (now - 2 * 86_400, now - 2 * 86_400))
    assert evict(tmp_path, max_bytes=1, keep=set(), now=now) == []  # dot names are no copies
    assert not scratch.exists()
    assert not partial.exists()
    assert fresh.exists()  # an archive being written right now stays
