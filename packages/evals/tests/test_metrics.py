"""Metrics against values worked out by hand.

The worked example: three gold targets in three layers, five hits.

    gold  T1 ui       web/Login.tsx   Login  L10-20
          T2 handler  api/login.py    login  L5-9
          T3 db       api/models.py   (whole file)

    hits  1 web/Login.tsx L1-5    same file as T1, misses its span
          2 api/login.py  L1-6    overlaps T2
          3 web/Login.tsx L18-30  overlaps T1
          4 other.py      L1-10   nothing
          5 api/models.py L100-120  in T3's file

At file level the hits rank files, each at its first hit: web/Login.tsx, api/login.py,
other.py, api/models.py.
"""

from __future__ import annotations

import math

import pytest

from sensei_evals.metrics import (
    Level,
    Span,
    abstention_accuracy,
    all_layers_hit_at_k,
    f_beta,
    mrr,
    ndcg_at_k,
    recall_at_k,
)
from sensei_evals.retrieval import Hit
from sensei_evals.schema import GoldTarget, Layer

T1 = GoldTarget(layer=Layer.UI, path="web/Login.tsx", symbol="Login", lines=(10, 20))
T2 = GoldTarget(layer=Layer.HANDLER, path="api/login.py", symbol="login", lines=(5, 9))
T3 = GoldTarget(layer=Layer.DB, path="api/models.py")
GOLD = [T1, T2, T3]
HITS = [
    Hit("web/Login.tsx", (1, 5)),
    Hit("api/login.py", (1, 6)),
    Hit("web/Login.tsx", (18, 30)),
    Hit("other.py", (1, 10)),
    Hit("api/models.py", (100, 120)),
]
LOG2_3 = math.log2(3)
LOG2_5 = math.log2(5)
LOG2_6 = math.log2(6)
IDEAL_3 = 1 + 1 / LOG2_3 + 1 / 2  # three new units at ranks 1, 2 and 3


@pytest.mark.parametrize(
    ("k", "file", "symbol"),
    # File level: api/models.py is the 4th file (hit 3 repeats web/Login.tsx).
    [(1, 1 / 3, 0.0), (2, 2 / 3, 1 / 3), (3, 2 / 3, 2 / 3), (4, 1.0, 2 / 3), (5, 1.0, 1.0)],
)
def test_recall_at_k(k: int, file: float, symbol: float) -> None:
    assert recall_at_k(HITS, GOLD, k, Level.FILE) == pytest.approx(file)
    assert recall_at_k(HITS, GOLD, k, Level.SYMBOL) == pytest.approx(symbol)


def test_recall_counts_each_gold_unit_once() -> None:
    gold = [T1, T1, T2.model_copy(update={"layer": Layer.SERVICE}), T2]
    # Units: T1's span and T2's span (the same span in two layers is one unit).
    assert recall_at_k(HITS, gold, 2, Level.SYMBOL) == pytest.approx(1 / 2)
    # Files: web/Login.tsx and api/login.py.
    assert recall_at_k(HITS, gold, 1, Level.FILE) == pytest.approx(1 / 2)


def test_whole_file_hit_does_not_cover_a_span() -> None:
    assert recall_at_k([Hit("web/Login.tsx")], [T1], 1, Level.SYMBOL) == 0.0
    assert recall_at_k([Hit("web/Login.tsx")], [T1], 1, Level.FILE) == 1.0
    assert recall_at_k([Hit("api/models.py")], [T3], 1, Level.SYMBOL) == 1.0


def test_span_edges_overlap_inclusively() -> None:
    assert recall_at_k([Hit("web/Login.tsx", (20, 25))], [T1], 1, Level.SYMBOL) == 1.0
    assert recall_at_k([Hit("web/Login.tsx", (1, 10))], [T1], 1, Level.SYMBOL) == 1.0
    assert recall_at_k([Hit("web/Login.tsx", (21, 25))], [T1], 1, Level.SYMBOL) == 0.0
    assert recall_at_k([Hit("web/Login.tsx", (1, 9))], [T1], 1, Level.SYMBOL) == 0.0


def test_ndcg_at_k() -> None:
    # File level: gold files at file ranks 1, 2 and 4 (other.py is 3rd).
    file_dcg = 1 + 1 / LOG2_3 + 1 / LOG2_5
    assert ndcg_at_k(HITS, GOLD, 5, Level.FILE) == pytest.approx(file_dcg / IDEAL_3)
    assert ndcg_at_k(HITS, GOLD, 5, Level.FILE) == pytest.approx(0.967_47, abs=1e-5)
    # Symbol level: T2 at rank 2, T1 at rank 3, T3 at rank 5.
    symbol_dcg = 1 / LOG2_3 + 1 / 2 + 1 / LOG2_6
    assert ndcg_at_k(HITS, GOLD, 5, Level.SYMBOL) == pytest.approx(symbol_dcg / IDEAL_3)
    assert ndcg_at_k(HITS, GOLD, 5, Level.SYMBOL) == pytest.approx(0.712_26, abs=1e-5)


def test_ndcg_ideal_is_capped_at_k() -> None:
    # Three units but k = 1: one perfect first hit is a perfect ranking.
    assert ndcg_at_k(HITS, GOLD, 1, Level.FILE) == 1.0
    assert ndcg_at_k(HITS, GOLD, 1, Level.SYMBOL) == 0.0


def test_a_perfect_ranking_is_one_with_two_targets_in_one_file() -> None:
    # Two symbols in a.py and one in b.py, found in that order: the files rank a.py, b.py.
    gold = [
        GoldTarget(layer=Layer.HANDLER, path="a.py", lines=(1, 5)),
        GoldTarget(layer=Layer.HANDLER, path="a.py", lines=(20, 30)),
        GoldTarget(layer=Layer.SERVICE, path="b.py", lines=(1, 9)),
    ]
    hits = [Hit(t.path, t.lines) for t in gold]
    for level in Level:
        assert ndcg_at_k(hits, gold, 10, level) == pytest.approx(1.0), level
        assert recall_at_k(hits, gold, 10, level) == 1.0
    assert mrr([Hit("x.py"), Hit("x.py"), Hit("a.py")], gold, Level.FILE) == 1 / 2


def test_one_hit_covering_nested_spans_earns_both() -> None:
    # A class and its method: the hit on the class covers both at rank 1, as well as possible.
    gold = [
        GoldTarget(layer=Layer.SERVICE, path="svc.py", symbol="Service", lines=(1, 100)),
        GoldTarget(layer=Layer.SERVICE, path="svc.py", symbol="Service.run", lines=(20, 30)),
    ]
    assert ndcg_at_k([Hit("svc.py", (1, 100))], gold, 10, Level.SYMBOL) == 1.0
    # Found later, it scores less: both units at rank 2 (2 / log2(3)) of the ideal 1 + 1/log2(3).
    late = ndcg_at_k([Hit("other.py", (1, 5)), Hit("svc.py", (1, 100))], gold, 10, Level.SYMBOL)
    assert late == pytest.approx((2 / LOG2_3) / (1 + 1 / LOG2_3))


def test_ndcg_perfect_ranking_is_one() -> None:
    hits = [Hit(t.path, t.lines) for t in GOLD]
    assert ndcg_at_k(hits, GOLD, 10, Level.SYMBOL) == pytest.approx(1.0)


def test_mrr() -> None:
    assert mrr(HITS, GOLD, Level.FILE) == 1.0
    assert mrr(HITS, GOLD, Level.SYMBOL) == 0.5
    assert mrr(HITS[3:4], GOLD, Level.SYMBOL) == 0.0
    assert mrr(HITS, GOLD, Level.SYMBOL, k=1) == 0.0


@pytest.mark.parametrize(
    ("k", "level", "expected"),
    [
        (1, Level.FILE, 0.0),  # ui only
        (2, Level.FILE, 0.0),  # ui + handler, no db
        (2, Level.SYMBOL, 0.0),  # handler only (rank 1 misses T1's span)
        (3, Level.SYMBOL, 0.0),  # ui + handler, no db
        (5, Level.SYMBOL, 1.0),
        (5, Level.FILE, 1.0),
    ],
)
def test_all_layers_hit_at_k(k: int, level: Level, expected: float) -> None:
    assert all_layers_hit_at_k(HITS, GOLD, k, level) == expected


def test_all_layers_needs_one_target_per_layer_not_all() -> None:
    other_ui = GoldTarget(layer=Layer.UI, path="web/Other.tsx", symbol="Other", lines=(1, 2))
    assert all_layers_hit_at_k(HITS, [T1, other_ui, T2], 3, Level.SYMBOL) == 1.0


def test_f_beta_file_level() -> None:
    # Top 3 hit 2 distinct files, both gold; gold has 3 files: P = 1, R = 2/3.
    # F0.5 = 1.25 * 1 * 2/3 / (0.25 + 2/3) = 10/11
    assert f_beta(HITS[:3], GOLD, 0.5, "file") == pytest.approx(10 / 11)


def test_f_beta_line_level() -> None:
    # Predicted lines: Login.tsx 1-5 and 18-30 (18) + login.py 1-6 (6) = 24.
    # Gold lines: Login.tsx 10-20 (11) + login.py 5-9 (5) = 16; models.py has no span.
    # Shared: Login.tsx 18-20 (3) + login.py 5-6 (2) = 5.  P = 5/24, R = 5/16.
    # F0.5 = 1.25 P R / (0.25 P + R) = 25/112;  F1 = 2 P R / (P + R) = 1/4.
    assert f_beta(HITS[:3], GOLD, 0.5, "line") == pytest.approx(25 / 112)
    assert f_beta(HITS[:3], GOLD, 1.0, "line") == pytest.approx(0.25)


def test_f_beta_edges() -> None:
    assert f_beta([], GOLD, 0.5, "file") == 0.0
    assert f_beta(HITS, [], 0.5, "file") is None
    assert f_beta(HITS, [T3], 0.5, "line") is None  # no gold span to score lines against
    assert f_beta([Span("x.py")], [Span("y.py")], 0.5, "file") == 0.0
    assert f_beta([Span("a.py", (1, 4))], [Span("a.py", (1, 4))], 0.5, "line") == 1.0


def test_metrics_are_undefined_without_gold() -> None:
    assert recall_at_k(HITS, [], 10) is None
    assert ndcg_at_k(HITS, [], 10) is None
    assert mrr(HITS, []) is None
    assert all_layers_hit_at_k(HITS, [], 10) is None


def test_abstention_accuracy() -> None:
    outcomes = [(True, False), (True, True), (False, True), (False, False)]
    assert abstention_accuracy(outcomes) == 0.5
    assert abstention_accuracy([(False, True), (True, False)]) == 1.0
    assert abstention_accuracy([]) is None
