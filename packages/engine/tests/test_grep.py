"""Exact search with ripgrep: literal, regex, globs and case on the pinned fixture repository,
then the edges (context, long lines, encodings), the safety rules and the time limit."""

from __future__ import annotations

import asyncio
import os
import tarfile
import time
from pathlib import Path

import pytest

from sensei_engine.ingest.discover import discover
from sensei_engine.search.grep import (
    MAX_LINE_CHARS,
    MAX_RECORD_BYTES,
    Case,
    ExactSearch,
    GrepBusyError,
    GrepQuery,
    GrepResult,
    GrepUnavailableError,
    InvalidQueryError,
)
from sensei_engine.snapshots import READY_MARKER

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fixture")
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(root, filter="data")
    return root / "fastapi-template"


@pytest.fixture(scope="module")
def recorded(repo: Path) -> frozenset[str]:
    return frozenset(file.path for file in discover(repo, max_files=10_000).files)


async def _search(root: Path, query: GrepQuery, files: frozenset[str] | None = None) -> GrepResult:
    return await ExactSearch().search(root, query, files=files)


def _places(result: GrepResult) -> list[tuple[str, int]]:
    return [(hit.path, hit.line) for hit in result.hits]


# ------------------------------------------------------------------ the fixture


async def test_a_literal_finds_every_line_in_path_order(repo: Path, recorded: frozenset[str]):
    result = await _search(repo, GrepQuery("SessionDep", globs=("backend/**",)), recorded)
    counts: dict[str, int] = {}
    for hit in result.hits:
        counts[hit.path] = counts.get(hit.path, 0) + 1
    assert counts == {
        "backend/app/api/deps.py": 2,
        "backend/app/api/routes/items.py": 6,
        "backend/app/api/routes/login.py": 5,
        "backend/app/api/routes/private.py": 2,
        "backend/app/api/routes/users.py": 10,
    }
    assert list(counts) == sorted(counts)  # by path
    assert (result.truncated, result.timed_out, result.long_lines) == (False, False, 0)
    first = result.hits[0]
    assert (first.path, first.line, first.col, first.text) == (
        "backend/app/api/deps.py",
        26,
        1,
        "SessionDep = Annotated[Session, Depends(get_db)]",
    )
    assert first.ranges == [(1, 11)]


async def test_a_literal_is_not_a_regex(repo: Path):
    literal = await _search(repo, GrepQuery("def read_items(", globs=("*.py",)))
    assert _places(literal) == [("backend/app/api/routes/items.py", 14)]
    with pytest.raises(InvalidQueryError, match="unclosed group"):
        await _search(repo, GrepQuery("def read_items(", regex=True))


async def test_a_regex(repo: Path, recorded: frozenset[str]):
    result = await _search(repo, GrepQuery(r"^def read_\w+", regex=True), recorded)
    assert _places(result) == [
        ("backend/app/api/routes/items.py", 14),
        ("backend/app/api/routes/items.py", 49),
        ("backend/app/api/routes/users.py", 37),
        ("backend/app/api/routes/users.py", 125),
        ("backend/app/api/routes/users.py", 163),
    ]
    assert [hit.ranges for hit in result.hits][2] == [(1, 15)]  # "def read_users"


ROUTES = {
    "backend/app/api/routes/items.py",
    "backend/app/api/routes/login.py",
    "backend/app/api/routes/users.py",
}
TSX = {"frontend/src/components/Admin/columns.tsx", "frontend/src/routes/_layout/admin.tsx"}


@pytest.mark.parametrize(
    ("globs", "expected"),
    [
        ((), {"backend/app/api/deps.py", *ROUTES, *TSX}),
        (("*.py",), {"backend/app/api/deps.py", *ROUTES}),
        (("backend/app/api/*.py",), {"backend/app/api/deps.py"}),  # * stays in one directory
        (("/backend/app/api/routes/**",), ROUTES),
        (("backend/**", "!**/routes/**"), {"backend/app/api/deps.py"}),
        (("*.tsx", "*.py"), {"backend/app/api/deps.py", *ROUTES, *TSX}),
        (("!*.py",), TSX),
        (("*.ts",), set[str]()),
    ],
)
async def test_path_globs(repo: Path, globs: tuple[str, ...], expected: set[str]):
    result = await _search(repo, GrepQuery("CurrentUser", globs=globs))
    assert {hit.path for hit in result.hits} == expected


async def test_case(repo: Path):
    def query(pattern: str, case: Case) -> GrepQuery:
        return GrepQuery(pattern, case=case, globs=("backend/app/api/deps.py",))

    sensitive = await _search(repo, query("sessiondep", Case.SENSITIVE))
    insensitive = await _search(repo, query("sessiondep", Case.INSENSITIVE))
    smart_lower = await _search(repo, query("sessiondep", Case.SMART))
    smart_upper = await _search(repo, query("Sessiondep", Case.SMART))
    assert (len(sensitive.hits), len(insensitive.hits)) == (0, 2)
    assert (len(smart_lower.hits), len(smart_upper.hits)) == (2, 0)


async def test_the_limit_says_whether_there_are_more(repo: Path, recorded: frozenset[str]):
    exactly = await _search(
        repo, GrepQuery("SessionDep", globs=("backend/**",), limit=25), recorded
    )
    assert (len(exactly.hits), exactly.truncated) == (25, False)
    fewer = await _search(repo, GrepQuery("SessionDep", globs=("backend/**",), limit=24), recorded)
    assert (len(fewer.hits), fewer.truncated, fewer.timed_out) == (24, True, False)
    assert _places(fewer) == _places(exactly)[:24]
    # More than the limit in one file: rg stops in it, and still says there are more.
    one_file = await _search(repo, GrepQuery("SessionDep", globs=("**/users.py",), limit=3))
    assert (len(one_file.hits), one_file.truncated) == (3, True)


async def test_only_recorded_files_count(repo: Path, recorded: frozenset[str]):
    everything = await _search(repo, GrepQuery("SessionDep"))
    only = await _search(repo, GrepQuery("SessionDep"), frozenset({"backend/app/api/deps.py"}))
    assert len(everything.hits) > len(only.hits) == 2
    assert {hit.path for hit in only.hits} == {"backend/app/api/deps.py"}
    assert {hit.path for hit in everything.hits} <= recorded


# ------------------------------------------------------------------ edges


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    return root


async def test_context_lines_around_each_hit(tmp_path: Path):
    lines = [b"one", b"two", b"hit three", b"hit four", b"five", b"six", b"seven", b"hit eight"]
    root = _tree(tmp_path / "r", {"a.txt": b"\n".join(lines) + b"\n"})
    result = await _search(root, GrepQuery("hit", context=2))
    assert [(hit.line, hit.before, hit.after) for hit in result.hits] == [
        (3, ["one", "two"], ["hit four", "five"]),
        (4, ["two", "hit three"], ["five", "six"]),
        (8, ["six", "seven"], []),
    ]
    first = await _search(root, GrepQuery("one", context=3))
    assert (first.hits[0].before, first.hits[0].after) == ([], ["two", "hit three", "hit four"])
    # The last hit before the limit keeps its context, though the search stops there.
    limited = await _search(root, GrepQuery("hit", context=1, limit=1))
    assert (limited.truncated, limited.hits[0].after) == (True, ["hit four"])


async def test_columns_count_characters_and_lines_lose_their_endings(tmp_path: Path):
    root = _tree(
        tmp_path / "r",
        {"u.txt": "naïve café = needle\r\n".encode(), "latin1.txt": b"caf\xe9 needle\n"},
    )
    result = await _search(root, GrepQuery("needle"))
    by_path = {hit.path: hit for hit in result.hits}
    assert (by_path["u.txt"].col, by_path["u.txt"].text) == (14, "naïve café = needle")
    assert by_path["u.txt"].ranges == [(14, 20)]
    assert (by_path["latin1.txt"].col, by_path["latin1.txt"].text) == (6, "caf� needle")


async def test_long_lines_are_cut_around_the_match_or_skipped(tmp_path: Path):
    wide = b"x" * 3000 + b" needle " + b"y" * 3000
    huge = b"z" * (MAX_RECORD_BYTES + 10) + b" needle"
    root = _tree(tmp_path / "r", {"a.min.js": wide + b"\n", "b.min.js": huge + b"\nneedle\n"})
    result = await _search(root, GrepQuery("needle"))
    assert _places(result) == [("a.min.js", 1), ("b.min.js", 2)]
    wide_hit = result.hits[0]
    assert wide_hit.col == 3002
    assert len(wide_hit.text) == MAX_LINE_CHARS
    assert wide_hit.text[wide_hit.col - 1 - wide_hit.offset :].startswith("needle")
    assert result.long_lines == 1  # the match on b.min.js's first line couldn't be read


# ------------------------------------------------------------------ safety


async def test_links_are_not_followed_out_of_the_tree(tmp_path: Path):
    outside = _tree(tmp_path / "outside", {"secret.txt": b"needle: not yours\n"})
    root = _tree(tmp_path / "r", {"in.txt": b"needle\n"})
    (root / "file-link.txt").symlink_to(outside / "secret.txt")
    (root / "dir-link").symlink_to(outside, target_is_directory=True)
    result = await _search(root, GrepQuery("needle"))
    assert _places(result) == [("in.txt", 1)]
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(FileNotFoundError):
        await _search(linked_root, GrepQuery("needle"))


async def test_ignore_files_and_hidden_files_and_the_ready_marker(tmp_path: Path):
    root = _tree(
        tmp_path / "r",
        {
            ".gitignore": b"*.log\n",
            ".rgignore": b"kept/\n",
            "app.log": b"needle\n",
            "kept/a.txt": b"needle\n",
            ".github/ci.yml": b"needle\n",
            READY_MARKER: b"needle\n",
        },
    )
    result = await _search(root, GrepQuery("needle"))
    assert {hit.path for hit in result.hits} == {".github/ci.yml", "app.log", "kept/a.txt"}
    # The marker stays out even when a glob asks for it.
    asked = await _search(root, GrepQuery("needle", globs=(READY_MARKER,)))
    assert asked.hits == []


@pytest.mark.parametrize(
    "pattern", ["--files", "-e", "--pre=sh", "$(touch pwned)", "`id`; rm -rf ."]
)
async def test_patterns_are_only_ever_patterns(tmp_path: Path, pattern: str):
    root = _tree(tmp_path / "r", {"a.txt": f"say {pattern} here\n".encode()})
    result = await _search(root, GrepQuery(pattern))
    assert [(hit.path, hit.text) for hit in result.hits] == [("a.txt", f"say {pattern} here")]
    assert not (root / "pwned").exists()
    globbed = await _search(root, GrepQuery("say", globs=("--files", "-x")))
    assert globbed.hits == []


async def test_patterns_that_are_refused(tmp_path: Path):
    root = _tree(tmp_path / "r", {"a.txt": b"x\n"})
    with pytest.raises(InvalidQueryError, match="size limit"):
        await _search(root, GrepQuery(r"(\w{100}){100}", regex=True))
    with pytest.raises(InvalidQueryError, match="glob"):
        await _search(root, GrepQuery("x", globs=("[",)))
    # A backtracking engine would take forever on the first line; rg's is linear in the input.
    root = _tree(tmp_path / "slow", {"a.txt": b"a" * 50_000 + b"!\naaa\n"})
    started = time.monotonic()
    result = await _search(root, GrepQuery(r"^(a+)+$", regex=True))
    assert (_places(result), result.timed_out) == ([("a.txt", 2)], False)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pattern": ""},
        {"pattern": "x" * 1001},
        {"pattern": "a\nb"},
        {"pattern": "a\0b"},
        {"pattern": "a", "globs": ("",)},
        {"pattern": "a", "globs": ("x" * 257,)},
        {"pattern": "a", "globs": ("*",) * 21},
        {"pattern": "a", "limit": 0},
        {"pattern": "a", "limit": 501},
        {"pattern": "a", "context": 11},
    ],
)
def test_queries_are_checked(kwargs: dict[str, object]):
    with pytest.raises(InvalidQueryError):
        GrepQuery(**kwargs)  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------ the time limit


def _fake_rg(tmp_path: Path, body: str, version: str = "ripgrep 14.1.1") -> Path:
    """A stand-in rg: answers --version like the real one, runs ``body`` for a search."""
    script = tmp_path / "fake-rg"
    script.write_text(
        f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "{version}"; exit 0; fi\n'
        f"echo $$ > {tmp_path / 'pid'}\n{body}\n"
    )
    script.chmod(0o755)
    return script


def _gone(pid_file: Path) -> bool:
    try:
        os.kill(int(pid_file.read_text()), 0)
    except ProcessLookupError:
        return True
    return False


MATCH = (
    '{"type":"begin","data":{"path":{"text":"./a.py"}}}\n'
    '{"type":"match","data":{"path":{"text":"./a.py"},"lines":{"text":"needle\\n"},'
    '"line_number":7,"absolute_offset":0,"submatches":[{"match":{"text":"needle"},"start":0,"end":6}]}}'
)


async def test_a_slow_search_stops_at_the_time_limit_with_what_it_found(tmp_path: Path):
    root = _tree(tmp_path / "r", {"a.py": b"needle\n"})
    script = _fake_rg(tmp_path, f"cat <<'EOF'\n{MATCH}\nEOF\nexec /bin/sleep 30")
    search = ExactSearch(binary=str(script), timeout=0.5)
    started = time.monotonic()
    result = await search.search(root, GrepQuery("needle"))
    assert time.monotonic() - started < 3
    assert (_places(result), result.timed_out, result.truncated) == ([("a.py", 7)], True, False)
    assert _gone(tmp_path / "pid")  # killed, not left running


async def test_a_search_that_never_answers_times_out_empty(tmp_path: Path):
    root = _tree(tmp_path / "r", {"a.py": b"needle\n"})
    search = ExactSearch(binary=str(_fake_rg(tmp_path, "exec /bin/sleep 30")), timeout=0.3)
    result = await search.search(root, GrepQuery("needle"))
    assert (result.hits, result.timed_out) == ([], True)
    assert _gone(tmp_path / "pid")


async def test_searches_wait_their_turn_for_at_most_the_time_limit(tmp_path: Path):
    root = _tree(tmp_path / "r", {"a.py": b"needle\n"})
    search = ExactSearch(timeout=0.3, max_concurrent=1)
    await search._slots.acquire()  # pyright: ignore[reportPrivateUsage] - one still running
    started = time.monotonic()
    with pytest.raises(GrepBusyError):
        await search.search(root, GrepQuery("needle"))
    assert 0.3 <= time.monotonic() - started < 1
    asyncio.get_running_loop().call_later(0.1, search._slots.release)  # pyright: ignore[reportPrivateUsage]
    assert _places(await search.search(root, GrepQuery("needle"))) == [("a.py", 1)]


async def test_without_ripgrep_or_when_it_fails(tmp_path: Path):
    root = _tree(tmp_path / "r", {"a.py": b"needle\n"})
    with pytest.raises(GrepUnavailableError, match="not installed"):
        await ExactSearch(binary="no-such-rg").search(root, GrepQuery("needle"))
    crashing = ExactSearch(binary=str(_fake_rg(tmp_path, "echo 'rg: boom' >&2; exit 2")))
    with pytest.raises(GrepUnavailableError, match="boom"):
        await crashing.search(root, GrepQuery("needle"))
    # An rg too old for the --json output read here is refused before it runs.
    old = ExactSearch(binary=str(_fake_rg(tmp_path, "exit 0", version="ripgrep 13.0.0")))
    with pytest.raises(GrepUnavailableError, match=r"ripgrep 14\.0 or later"):
        await old.search(root, GrepQuery("needle"))


async def test_files_over_the_size_limit_are_not_searched(tmp_path: Path):
    big = b"needle\n" + b"x" * (2 * 1024 * 1024) + b"\n"
    root = _tree(tmp_path / "r", {"small.txt": b"needle\n", "huge.min.js": big})
    result = await _search(root, GrepQuery("needle"))
    assert _places(result) == [("small.txt", 1)]
