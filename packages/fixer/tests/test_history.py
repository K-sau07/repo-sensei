"""The history over a shallow checkout (8.5): blame says where the fetched history stops, that
commit is never a suspect, a suspect is its full id, and output is capped as it's read."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sensei_fixer import history as history_module
from sensei_fixer.history import GitHistory, HistoryError


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com",  # noqa: S607
         "-c", "protocol.file.allow=always", *args],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def shallow(tmp_path: Path) -> tuple[Path, list[str]]:
    """A depth-2 clone of a three-commit history: (its root, the commits oldest first)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    shas: list[str] = []
    for n, text in enumerate(["a = 1\n", "a = 1\nb = 2\n", "a = 1\nb = 2\nc = 3\n"]):
        (origin / "m.py").write_text(text)
        _git(origin, "add", "-A")
        _git(origin, "commit", "-q", "-m", f"step {n}")
        shas.append(_git(origin, "rev-parse", "HEAD"))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth=2", f"file://{origin}", str(clone))
    return clone, shas


async def test_where_a_shallow_history_stops_is_said_and_never_a_suspect(
    shallow: tuple[Path, list[str]],
) -> None:
    root, (first, second, third) = shallow
    history = GitHistory(root, third)
    assert await history.shallow() == {second}
    lines = await history.blame("m.py", 1, 3)
    # Lines older than the fetched history are credited to its oldest commit, marked so.
    assert [(b.sha, b.boundary) for b in lines] == [(second, True), (second, True),
                                                    (third, False)]  # fmt: skip
    assert await history.resolve(third[:9].upper()) == third  # the full id
    assert await history.resolve(second) is None  # it stands for every older commit
    assert await history.resolve(first) is None  # not fetched at all
    for wrong in (5, None, ["m.py"]):
        with pytest.raises(HistoryError):
            await history.show(third, wrong if wrong is not None else 7)
        with pytest.raises(HistoryError):
            await history.blame(wrong, 1, 1)  # type: ignore[arg-type]


async def test_output_is_capped_as_it_is_read(
    shallow: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, shas = shallow
    monkeypatch.setattr(history_module, "MAX_OUTPUT_BYTES", 40)
    shown = await GitHistory(root, shas[-1]).show(shas[-1])
    assert len(shown.encode()) == 40  # the rest isn't read, and cutting it short isn't an error
