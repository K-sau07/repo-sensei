"""The snapshot archive and the working-copy cache."""

from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path

import zstandard

from sensei_workers.ingest.archive import write_archive
from sensei_workers.ingest.cache import evict
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
