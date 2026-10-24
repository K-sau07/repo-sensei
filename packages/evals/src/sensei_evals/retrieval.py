"""What the harness evaluates: a retriever that turns a query into ranked hits.

Real retrievers (BM25, dense, hybrid, ...) arrive with the steps that build them and register in
:data:`RETRIEVERS`; the first is ``symbols``, 2.4's symbol lookup (:mod:`sensei_evals.symbols`).
The two stubs here validate the harness itself: the oracle returns exactly the gold (every
metric is 1.0) and the random retriever returns ``k`` files drawn uniformly (file recall@k is
``k / N`` on average over a corpus of ``N`` files).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sensei_engine.ingest.discover import discover
from sensei_evals.schema import GoldenItem, Suite

MAX_CORPUS_FILES = 500_000


@dataclass(frozen=True, slots=True)
class Hit:
    """One retrieved location. ``lines`` are 1-based and inclusive; ``None`` means the whole
    file (a file-level result, which never counts as covering a symbol's span)."""

    path: str
    lines: tuple[int, int] | None = None
    score: float = 0.0
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class RepoRef:
    """The benchmark repository being searched: its id in the suite, the pinned commit and,
    when a retriever needs files, a checkout of that commit."""

    id: str
    sha: str
    checkout: Path | None = None


class Retriever(Protocol):
    """Optionally also ``async prepare(repo)``, awaited before the first search (and outside
    its timing: building an index), and ``async aclose()``, awaited after the last."""

    name: str

    async def search(self, query: str, repo: RepoRef, k: int) -> list[Hit]:
        """The best ``k`` hits for ``query``, best first. An empty list means "nothing found",
        which the harness counts as abstaining."""
        ...


@dataclass(frozen=True, slots=True)
class RetrieverContext:
    """What a retriever factory gets to build its retriever from."""

    suite: Suite
    repo: RepoRef
    seed: int = 0
    options: dict[str, str] = field(default_factory=dict[str, str])


class OracleRetriever:
    """Returns each item's gold targets: an upper bound that checks the metrics end to end."""

    name = "oracle"

    def __init__(self, items: Sequence[GoldenItem]) -> None:
        self._gold = {(item.repo, item.query): item for item in items}

    async def search(self, query: str, repo: RepoRef, k: int) -> list[Hit]:
        item = self._gold.get((repo.id, query))
        if item is None:
            return []
        hits = [
            Hit(path=target.path, lines=target.lines, score=1.0, symbol=target.symbol)
            for target in item.gold
        ]
        return hits[:k]


class RandomRetriever:
    """``k`` distinct hits drawn uniformly from a fixed corpus; the same query and seed always
    draw the same hits."""

    name = "random"

    def __init__(self, corpus: Sequence[Hit], *, seed: int = 0) -> None:
        self._corpus = list(corpus)
        self._seed = seed

    async def search(self, query: str, repo: RepoRef, k: int) -> list[Hit]:
        # A string seed is hashed with SHA-512, so draws don't depend on PYTHONHASHSEED.
        rng = random.Random(f"{self._seed}:{repo.id}:{repo.sha}:{query}")  # noqa: S311
        return rng.sample(self._corpus, min(k, len(self._corpus)))


def file_corpus(checkout: Path) -> list[Hit]:
    """Every text file the indexer keeps under ``checkout``, each as one whole-file hit with its
    line span."""
    discovery = discover(checkout, max_files=MAX_CORPUS_FILES)
    return [
        Hit(path=file.path, lines=(1, file.loc))
        for file in discovery.files
        if file.loc  # binary files have no line count, empty files have none to cover
    ]


def _oracle(context: RetrieverContext) -> Retriever:
    return OracleRetriever(context.suite.items)


def _random(context: RetrieverContext) -> Retriever:
    if context.repo.checkout is None:
        msg = "the random retriever needs a checkout of the repository (--checkout)"
        raise ValueError(msg)
    return RandomRetriever(file_corpus(context.repo.checkout), seed=context.seed)


def _symbols(context: RetrieverContext) -> Retriever:
    from sensei_evals.symbols import checkout_symbols  # noqa: PLC0415 - imports this module

    return checkout_symbols(context)


RetrieverFactory = Callable[[RetrieverContext], Retriever]

RETRIEVERS: dict[str, RetrieverFactory] = {
    "oracle": _oracle,
    "random": _random,
    "symbols": _symbols,
}
