"""The hybrid retriever's eval pieces (2.9): its options, its hits as scored, and the weight sweep's
grid and choice (the rankings themselves are read on an index: see the engine's tests)."""

from __future__ import annotations

import math
import uuid

import pytest

from sensei_engine.search.hybrid import SearchHit, Weights
from sensei_engine.search.plan import Leg, Origin
from sensei_engine.search.route import Lane, Layer
from sensei_evals import sweep
from sensei_evals.hybrid import to_hit, weights_from
from sensei_evals.retrieval import Hit


def test_options_set_leg_and_origin_weights() -> None:
    weights = weights_from({"w.grep": "0", "o.route": "0.5", "multi_hit": "0", "db": "x"})
    assert (weights.legs[Leg.GREP], weights.origins[Origin.ROUTE], weights.multi_hit) == (0, 0.5, 0)
    assert weights.legs[Leg.SYM] == Weights().legs[Leg.SYM]
    with pytest.raises(ValueError, match="nope"):
        weights_from({"w.nope": "1"})


def _hit(lines: tuple[int, int], chunk: tuple[int, int]) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        symbol_key="py a.py/f().",
        symbol_id=None,
        name="f",
        kind="function",
        path="a.py",
        language="python",
        layer=Layer.SERVICE,
        lane=Lane.SVC,
        start_line=lines[0],
        end_line=lines[1],
        chunk_start=chunk[0],
        chunk_end=chunk[1],
        score=1.0,
        legs={},
        chunks=1,
    )


def test_a_hit_is_scored_on_its_definition_and_its_snippet() -> None:
    # A part of a long definition: the definition; one of several small ones: their chunk.
    assert to_hit(_hit((10, 90), (40, 60))) == Hit("a.py", (10, 90), 1.0, "py a.py/f().")
    assert to_hit(_hit((4, 7), (1, 20))).lines == (1, 20)


def test_the_grid_covers_every_combination_from_the_base() -> None:
    base = Weights()
    points = list(sweep.grid(base))
    size = math.prod(len(values) for values in sweep.GRID.values())
    assert len(points) == len(set(map(sweep.describe, points))) == size
    assert {point.legs[Leg.SEM_CODE] for point in points} == set(sweep.GRID["sem_code"])
    assert all(point.legs[Leg.SEM_SUMMARY] == base.legs[Leg.SEM_SUMMARY] for point in points)


def test_equal_scores_keep_the_weights_nearest_the_base(monkeypatch: pytest.MonkeyPatch) -> None:
    def flat(collected: object, weights: Weights, view: str) -> dict[str, float]:
        return dict.fromkeys(sweep.REPORTED, 0.5)

    monkeypatch.setattr(sweep, "evaluate", flat)
    base = Weights(legs={**Weights().legs, Leg.LEX_CODE: 1.0, Leg.GREP: 0.5}, multi_hit=0.1)
    best = sweep.sweep({"suite": []}, base)[0]
    assert sweep.describe(best.weights) == sweep.describe(base)
