"""The search service (2.13): snippets and their highlights, the modes, the pivots to exact search,
and the reranked order and its scores (the endpoint's own tests run it on an index)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.search.hybrid import SearchHit
from sensei_engine.search.plan import Leg, plan
from sensei_engine.search.rerank import Candidate, Ranked, Ranking, Reranker, RerankMode
from sensei_engine.search.route import Lane, Layer, classify
from sensei_engine.search.service import (
    ChunkText,
    ExactQuery,
    Result,
    SearchMode,
    SemanticNotReadyError,
    exact_queries,
    interpret,
    narrow,
    rerank,
    snippet,
    terms_of,
)


def _result(start: int, end: int, chunk: tuple[int, int]) -> Result:
    return Result(
        id="py a.py/f().",
        chunk_id=uuid.uuid4(),
        symbol_key="py a.py/f().",
        name="f",
        kind="function",
        path="a.py",
        language="python",
        lane=Lane.SVC,
        start_line=start,
        end_line=end,
        chunk_start=chunk[0],
        chunk_end=chunk[1],
        score=1.0,
        why=("keyword",),
    )


def test_highlights_are_in_utf16_columns() -> None:
    text = ChunkText(10, ("emoji = '😀' + token_value", "other"))
    found = snippet(_result(10, 11, (10, 11)), text, ["token_value"])
    assert found.start_line == 10
    # The emoji is one character and two UTF-16 code units.
    assert [(h.line, h.start, h.end) for h in found.highlights] == [(10, 15, 26)]
    assert found.lines == ("emoji = '😀' + token_value", "other")


def test_a_snippet_starts_just_before_the_first_line_with_a_term() -> None:
    lines = (*(f"line {n}" for n in range(1, 41)), "the needle is here")
    text = ChunkText(1, lines)
    found = snippet(_result(1, 41, (1, 41)), text, ["NEEDLE"])  # any case
    assert len(found.lines) == 12
    # As many lines before the match as fit, up to the end of the result.
    assert (found.start_line, found.lines[-1]) == (30, "the needle is here")
    assert [(h.line, h.start, h.end) for h in found.highlights] == [(41, 4, 10)]
    quiet = snippet(_result(5, 8, (1, 41)), text, ["absent"])
    assert (quiet.start_line, quiet.lines) == (5, ("line 5", "line 6", "line 7", "line 8"))
    assert quiet.highlights == ()


def test_overlapping_terms_highlight_once() -> None:
    text = ChunkText(1, ("create_item(item)",))
    found = snippet(_result(1, 1, (1, 1)), text, ["item", "create_item"])
    assert [(h.start, h.end) for h in found.highlights] == [(0, 11), (12, 16)]


def test_modes_narrow_the_plan() -> None:
    found = plan(classify("how is a new item created?").route)
    assert {sub.leg for sub in found.legs} & {Leg.SEM_CODE, Leg.SEM_SUMMARY}
    keyword = narrow(found, SearchMode.KEYWORD, semantic_ready=False)
    assert not {sub.leg for sub in keyword.legs} & {Leg.SEM_CODE, Leg.SEM_SUMMARY}
    with pytest.raises(SemanticNotReadyError):
        narrow(found, SearchMode.SEMANTIC, semantic_ready=False)
    semantic = narrow(found, SearchMode.SEMANTIC, semantic_ready=True)
    assert {sub.leg for sub in semantic.legs} <= {Leg.SEM_CODE, Leg.SEM_SUMMARY}
    # A symbol query's plan has no semantic leg: its words are embedded instead.
    named = narrow(plan(classify("create_item").route), SearchMode.SEMANTIC, semantic_ready=True)
    assert [(sub.leg, sub.text) for sub in named.legs] == [(Leg.SEM_CODE, "create_item")]


def test_the_interpretation_and_the_pivots_to_exact() -> None:
    route = classify('where is "session.add(item)" used in backend/app/').route
    found = plan(route)
    meaning = interpret(route, found, None)
    assert meaning.query_class == "literal"
    assert meaning.literals == ("session.add(item)", "backend/app/")
    assert exact_queries(route)[:1] == (ExactQuery("literal", "session.add(item)"),)
    assert ExactQuery("path", "backend/app/") in exact_queries(route)
    assert "session.add(item)" in terms_of(route)
    assert interpret(classify("/def \\w+_item/").route, found, "a/").query_class == "regex"
    symbol = classify("create_item").route
    assert exact_queries(symbol)[0] == ExactQuery("symbol", "create_item")
    assert "where" not in terms_of(route)


def _hit(n: int, score: float) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_OID, str(n)),
        symbol_key=f"py m.py/f{n}().",
        symbol_id=None,
        name=f"f{n}",
        kind="function",
        path="m.py",
        language="python",
        layer=Layer.SERVICE,
        lane=Lane.SVC,
        start_line=n,
        end_line=n,
        chunk_start=n,
        chunk_end=n,
        score=score,
        legs={"lex_code": n + 1},
        chunks=1,
    )


class _Reranker(Reranker):
    """The cross-encoder's answer, made up: it keeps ``order`` with ``scores``."""

    def __init__(self, order: Sequence[int], scores: Sequence[float], *, scored: bool) -> None:
        self._order = order
        self._scores = scores
        self._scored = scored
        self.seen: list[Candidate] = []

    async def rerank(
        self, query: str, candidates: Sequence[Candidate], mode: RerankMode, **_: Any
    ) -> Ranking:
        assert mode is RerankMode.INTERACTIVE
        self.seen = list(candidates)
        items = [
            Ranked(candidates[n], score) for n, score in zip(self._order, self._scores, strict=True)
        ]
        return Ranking(tuple(items), "fallback", scored=self._scored)


async def test_reranked_results_lead_with_their_relevance_and_the_rest_follow_below() -> None:
    hits = [_hit(n, 1.0 / (n + 1)) for n in range(5)]
    reranker = _Reranker([2, 0], [0.9, 0.4], scored=True)
    ordered, scores, reranked = await rerank(reranker, "q", hits, {}, candidates=3)
    assert reranked
    assert len(reranker.seen) == 3  # only the best go to the cross-encoder
    # Its picks first, then what it was shown but dropped, then the rest, in fused order.
    assert [hit.start_line for hit in ordered] == [2, 0, 1, 3, 4]
    assert scores[:2] == [0.9, 0.4]
    assert scores[2] < 0.4
    assert scores[2:] == sorted(scores[2:], reverse=True)

    assert {c.graph_neighbourhood for c in reranker.seen} == {None}  # no graph given

    unscored = _Reranker([2, 0], [0.9, 0.4], scored=False)
    ordered, scores, reranked = await rerank(unscored, "q", hits, {}, candidates=3)
    assert not reranked
    assert [hit.start_line for hit in ordered] == [0, 1, 2, 3, 4]
    assert scores[0] == 1.0


async def test_the_reranker_reads_each_candidates_callers_and_callees() -> None:
    hits = [_hit(n, 1.0 / (n + 1)) for n in range(3)]
    nodes = [
        Node(f"py m.py/f{n}().", "function", f"f{n}", f"f{n}", "m.py", n, n, "python", "svc")
        for n in range(3)
    ]
    nodes.append(
        Node("py db.py/save().", "function", "save", "save", "db.py", 1, 2, "python", "db")
    )
    calls = Edge(EdgeType.CALLS, 0.9, Provenance.IMPORT_RESOLVED, "m.py", 1)
    graph = GraphSnapshot(nodes, [(0, 1, calls), (1, 3, calls)])
    reranker = _Reranker([1, 0], [0.9, 0.4], scored=True)
    await rerank(reranker, "q", hits, {}, candidates=3, graph=graph)
    assert [c.graph_neighbourhood for c in reranker.seen] == [
        "calls: f1",
        "calls: save [db]\ncalled by: f0",
        None,  # nothing calls f2, and it calls nothing
    ]
