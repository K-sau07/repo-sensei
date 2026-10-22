"""Retrieval and answer metrics, computed per item (the runner averages them).

Two levels of matching:

* **file**: the gold units are the distinct gold paths; a hit covers one if it's in that file.
  The hits are taken as a ranking of files: each file at its first hit (several chunks of one
  file are one result here).
* **symbol**: the gold units are the distinct gold targets; a hit covers a target with lines if
  it's in the same file and its lines overlap the target's span (a whole-file hit doesn't count),
  and covers a target without lines (a whole file) if it's in that file.

Every metric returns ``None`` when it is undefined for the item (no gold to find), so averages
only count the items a metric applies to.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from sensei_evals.retrieval import Hit
from sensei_evals.schema import GoldTarget, Layer


class Level(StrEnum):
    FILE = "file"
    SYMBOL = "symbol"


class Located(Protocol):
    """A file, or a span of lines in one (1-based, inclusive)."""

    @property
    def path(self) -> str: ...

    @property
    def lines(self) -> tuple[int, int] | None: ...


@dataclass(frozen=True, slots=True)
class Span:
    path: str
    lines: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class _Unit:
    path: str
    lines: tuple[int, int] | None


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _units(gold: Iterable[GoldTarget], level: Level) -> list[_Unit]:
    units: dict[_Unit, None] = {}  # insertion-ordered set
    for target in gold:
        lines = target.lines if level is Level.SYMBOL else None
        units[_Unit(target.path, lines)] = None
    return list(units)


def _covers(hit: Hit, unit: _Unit) -> bool:
    if hit.path != unit.path:
        return False
    if unit.lines is None:
        return True
    return hit.lines is not None and _overlap(hit.lines, unit.lines)


def covered(hits: Sequence[Hit], gold: Sequence[GoldTarget], level: Level) -> list[GoldTarget]:
    """The gold targets the hits cover at ``level`` (file level: every target in a hit file)."""
    return [
        target
        for target in gold
        if any(
            _covers(hit, _Unit(target.path, target.lines if level is Level.SYMBOL else None))
            for hit in hits
        )
    ]


def _ranked(hits: Sequence[Hit], level: Level) -> Sequence[Hit]:
    """The hits as the level ranks them: at file level, each file once, at its first hit."""
    if level is not Level.FILE:
        return hits
    seen: set[str] = set()
    files: list[Hit] = []
    for hit in hits:
        if hit.path not in seen:
            seen.add(hit.path)
            files.append(hit)
    return files


def recall_at_k(
    hits: Sequence[Hit], gold: Sequence[GoldTarget], k: int, level: Level = Level.FILE
) -> float | None:
    """The share of gold units covered by the top ``k`` hits (files, at file level)."""
    units = _units(gold, level)
    if not units:
        return None
    top = _ranked(hits, level)[:k]
    found = sum(1 for unit in units if any(_covers(hit, unit) for hit in top))
    return found / len(units)


def ndcg_at_k(
    hits: Sequence[Hit], gold: Sequence[GoldTarget], k: int, level: Level = Level.FILE
) -> float | None:
    """nDCG over gold units: a hit gains the number of gold units it covers that no earlier hit
    did (discounted by ``log2(rank + 1)``), so repeats of a found file or span earn nothing, and
    one hit covering nested spans (a class and its method) earns both. The ideal ranking puts one
    new unit at each of the first ``min(units, k)`` ranks; a hit covering several at once can
    beat it, so the score is capped at 1. A ranking that finds every unit as early as it could
    scores 1."""
    units = _units(gold, level)
    if not units:
        return None
    seen: set[_Unit] = set()
    dcg = 0.0
    for rank, hit in enumerate(_ranked(hits, level)[:k], start=1):
        new = [unit for unit in units if unit not in seen and _covers(hit, unit)]
        if new:
            dcg += len(new) / math.log2(rank + 1)
            seen.update(new)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(units), k) + 1))
    return min(1.0, dcg / ideal)


def mrr(
    hits: Sequence[Hit],
    gold: Sequence[GoldTarget],
    level: Level = Level.FILE,
    k: int | None = None,
) -> float | None:
    """Reciprocal rank of the first hit (file, at file level) covering any gold unit (0 if none
    in the top ``k``)."""
    units = _units(gold, level)
    if not units:
        return None
    for rank, hit in enumerate(_ranked(hits, level)[:k], start=1):
        if any(_covers(hit, unit) for unit in units):
            return 1.0 / rank
    return 0.0


def all_layers_hit_at_k(
    hits: Sequence[Hit], gold: Sequence[GoldTarget], k: int, level: Level = Level.SYMBOL
) -> float | None:
    """1.0 if the top ``k`` hits cover at least one gold target in every layer the gold spans
    (both the UI side and the API side of a flow, say), else 0.0."""
    by_layer: dict[Layer, list[GoldTarget]] = {}
    for target in gold:
        by_layer.setdefault(target.layer, []).append(target)
    if not by_layer:
        return None
    top = hits[:k]
    for targets in by_layer.values():
        if not any(_covers(hit, unit) for unit in _units(targets, level) for hit in top):
            return 0.0
    return 1.0


def _lines(spans: Iterable[Located]) -> set[tuple[str, int]]:
    lines: set[tuple[str, int]] = set()
    for span in spans:
        if span.lines is not None:
            start, end = span.lines
            lines.update((span.path, n) for n in range(start, end + 1))
    return lines


def _keys(spans: Iterable[Located], level: Literal["file", "line"]) -> set[tuple[str, int]]:
    """Distinct files (as ``(path, 0)``) or distinct ``(path, line)`` pairs."""
    if level == "file":
        return {(span.path, 0) for span in spans}
    return _lines(spans)


def f_beta(
    pred_spans: Iterable[Located],
    gold_spans: Iterable[Located],
    beta: float = 0.5,
    level: Literal["file", "line"] = "file",
) -> float | None:
    """F-beta of predicted against gold locations, over distinct files or distinct
    ``(file, line)`` pairs; beta 0.5 weighs precision over recall. Spans without lines are
    left out at line level. ``None`` without gold, 0.0 without predictions."""
    predicted = _keys(pred_spans, level)
    expected = _keys(gold_spans, level)
    if not expected:
        return None
    true_positives = len(predicted & expected)
    if true_positives == 0:
        return 0.0
    precision = true_positives / len(predicted)
    recall = true_positives / len(expected)
    b2 = beta * beta
    return (1 + b2) * precision * recall / (b2 * precision + recall)


def abstention_accuracy(outcomes: Iterable[tuple[bool, bool]]) -> float | None:
    """Share of ``(answerable, abstained)`` outcomes that are right: answered when the code has
    the answer, abstained when it doesn't."""
    results = [abstained != answerable for answerable, abstained in outcomes]
    if not results:
        return None
    return sum(results) / len(results)
