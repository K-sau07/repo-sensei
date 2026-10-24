"""A size, time and cost estimate for indexing a repository, before connecting it.

**Files** come from the default branch's git tree when GitHub gives it (:func:`tree_stats`): its
file count was within 4% of what discovery keeps on four repositories (fastapi 3,233 vs 3,232,
full-stack-fastapi-template 252 vs 245, langflow 11,850 vs 11,407, flask-api 89 vs 87). Without
it, GitHub's per-language bytes are the basis: they count code only (no docs, data or assets),
so code files are guessed at :data:`CODE_BYTES_PER_FILE` (fastapi-template 2.7 KB, langflow
7.1 KB) and all files from :data:`CODE_SHARE`. GitHub's ``size`` (the repository on disk,
history included) is the last resort.

**Time and cost** are per stage, from measured runs (2026-10):

- clone: ~1.3 s plus ~0.075 s a MB (flask-api 0.5 MB in 1.3 s, fastapi 35 MB in 3.9 s);
- discover: ~0.4 ms a file (langflow, 11,407 files: 4.3 s);
- parse and symbols: ~0.8 ms a code file (langflow, 8,197: 4.3 s on 10 cores; doubled for a worker)
  and ~0.07 ms a symbol saved;
- embeddings: ~7 chunks a code file (the symbols that get summaries, ~6 a file, plus the file), at
  ~330 tokens a chunk with voyage-code-4: about $0.04 per thousand chunks;
- summaries (Haiku, batch): ~2,600 input and ~540 output tokens a code file (fastapi-template:
  152 files for $0.035, the batch done in 3.6 minutes).

Rounded and labelled estimates in the UI.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, cast

from sensei_core.usage import CostCalculator

SOURCE_SHARE = 0.1  # of the on-disk size, when GitHub reports no languages
CODE_BYTES_PER_FILE = 4_000  # of language bytes, without a tree
CODE_SHARE = 0.7  # of files that are code, without a tree (fastapi-template 0.62, langflow 0.72)
TREE_KEPT = 0.97  # of the tree's files that discovery keeps
CHARS_PER_TOKEN = 3.5
CODE_EXTENSIONS = frozenset(
    [
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".vue",
        ".svelte",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".cs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".swift",
        ".m",
        ".mm",
    ]
)

# Seconds per stage (see the module).
CLONE_SECONDS = 1.3
CLONE_SECONDS_PER_MB = 0.075
DISCOVER_SECONDS = 0.25
DISCOVER_SECONDS_PER_FILE = 0.0004
PARSE_SECONDS = 0.5
PARSE_SECONDS_PER_CODE_FILE = 0.0008
SAVE_SECONDS_PER_SYMBOL = 0.00007
SYMBOLS_PER_CODE_FILE = 6
CHUNKS_PER_CODE_FILE = 7
TOKENS_PER_CHUNK = 330
EMBED_CHUNKS_PER_SECOND = 400  # 128 a request, 4 at once, ~1 s each
SUMMARY_INPUT_TOKENS_PER_FILE = 2_600
SUMMARY_OUTPUT_TOKENS_PER_FILE = 540
SUMMARY_BATCH_SECONDS = 200  # a small batch's wait; most end within the hour
SUMMARY_SECONDS_PER_FILE = 0.1
EMBED_MODEL = "voyage-code-4"
SUMMARY_MODEL = "claude-haiku-5-5"


@dataclass(frozen=True, slots=True)
class TreeStats:
    """The default branch's files, from GitHub's recursive git tree."""

    files: int
    code_files: int
    code_bytes: int
    bytes: int
    truncated: bool  # GitHub stops at 100,000 entries (or 7 MB): the counts are a floor


@dataclass(frozen=True, slots=True)
class IndexEstimate:
    files: int
    tokens: int
    index_minutes: float
    cost_usd: Decimal


def tree_stats(body: Mapping[str, Any]) -> TreeStats | None:
    """Counts from a ``GET /repos/{repo}/git/trees/{ref}?recursive=1`` answer (None if it
    isn't one)."""
    entries = body.get("tree")
    if not isinstance(entries, list):
        return None
    files = code_files = code_bytes = total = 0
    for entry in _objects(cast("list[Any]", entries)):
        if entry.get("type") != "blob":
            continue
        size = entry.get("size")
        size = size if isinstance(size, int) and size >= 0 else 0
        files += 1
        total += size
        if PurePosixPath(str(entry.get("path", ""))).suffix.lower() in CODE_EXTENSIONS:
            code_files += 1
            code_bytes += size
    return TreeStats(files, code_files, code_bytes, total, bool(body.get("truncated")))


def _objects(values: list[Any]) -> Iterable[Mapping[str, Any]]:
    return (cast("Mapping[str, Any]", value) for value in values if isinstance(value, dict))


def estimate_index(
    size_kb: int,
    languages: dict[str, int],
    calc: CostCalculator,
    tree: TreeStats | None = None,
) -> IndexEstimate:
    language_bytes = sum(size for size in languages.values() if size > 0)
    disk_bytes = max(0, size_kb) * 1024
    source_bytes = language_bytes or disk_bytes * SOURCE_SHARE
    guessed_code = round(source_bytes / CODE_BYTES_PER_FILE)
    guessed_files = max(1, round(guessed_code / CODE_SHARE))
    if tree is not None and tree.files:
        files = max(1, round(tree.files * TREE_KEPT))
        code_files, code_bytes, repo_bytes = tree.code_files, tree.code_bytes, tree.bytes
        if tree.truncated:  # a floor: the languages may know of more
            files = max(files, guessed_files)
            code_files = max(code_files, guessed_code)
            code_bytes = max(code_bytes, round(language_bytes))
    else:
        files, code_files, code_bytes = guessed_files, guessed_code, round(source_bytes)
        repo_bytes = disk_bytes
    tokens = round(code_bytes / CHARS_PER_TOKEN)
    chunks = code_files * CHUNKS_PER_CODE_FILE
    seconds = (
        CLONE_SECONDS
        + CLONE_SECONDS_PER_MB * repo_bytes / 1_000_000
        + DISCOVER_SECONDS
        + DISCOVER_SECONDS_PER_FILE * files
        + PARSE_SECONDS
        + PARSE_SECONDS_PER_CODE_FILE * code_files
        + SAVE_SECONDS_PER_SYMBOL * SYMBOLS_PER_CODE_FILE * code_files
        + chunks / EMBED_CHUNKS_PER_SECOND
        + (SUMMARY_BATCH_SECONDS + SUMMARY_SECONDS_PER_FILE * code_files if code_files else 0)
    )
    minutes = round(seconds / 60, 1)
    embed = calc.cost("voyage", EMBED_MODEL, tokens_in=chunks * TOKENS_PER_CHUNK) or Decimal(0)
    # Priced a file at a time (each request is short; the total isn't one long prompt).
    per_file = calc.cost(
        "anthropic",
        SUMMARY_MODEL,
        tokens_in=SUMMARY_INPUT_TOKENS_PER_FILE,
        tokens_out=SUMMARY_OUTPUT_TOKENS_PER_FILE,
        batch=True,
    ) or Decimal(0)
    compute = calc.cost("sensei", "index-worker", units=Decimal(str(minutes))) or Decimal(0)
    return IndexEstimate(
        files=files,
        tokens=tokens,
        index_minutes=minutes,
        cost_usd=embed + per_file * code_files + compute,
    )
