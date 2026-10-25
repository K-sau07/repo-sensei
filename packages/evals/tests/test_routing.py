"""The query router's rules on the private golden suites (skipped where they aren't), and the
router eval's report."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator
from sensei_engine.llm.anthropic_client import AnthropicClient
from sensei_engine.search.route import QueryRouter, classify
from sensei_evals import routing
from sensei_evals.runner import RunRecord
from sensei_evals.schema import load_suite
from sensei_testing.anthropic import settings


def _golden_suites() -> list[Path]:
    root = Path(__file__).resolve().parents[3] / "evals" / "private" / "golden"
    return sorted(root.glob("*.yaml")) if root.is_dir() else []


@pytest.mark.skipif(not _golden_suites(), reason="the private golden suites aren't here")
def test_on_the_golden_suites_what_the_rules_decide_is_right() -> None:
    items = [item for path in _golden_suites() for item in load_suite(path).items]
    answerable = [item for item in items if item.class_.value != "unanswerable"]
    decided_wrong: list[str] = []
    right = 0
    for item in answerable:
        classified = classify(item.query)
        right += classified.route.query_class.value == item.class_.value
        if classified.decided and classified.route.query_class.value != item.class_.value:
            decided_wrong.append(item.id)
    # A class the rules decide skips the model, so it has to be right.
    assert decided_wrong == []
    # Rules alone (the fallback) stay a usable router.
    assert right / len(answerable) >= 0.85


async def test_the_report_scores_answerable_queries_and_renders_a_confusion_matrix(
    tmp_path: Path,
) -> None:
    extra = tmp_path / "queries.yaml"
    extra.write_text(
        "- {class: symbol, query: useCartStore}\n"
        "- {class: impact, query: 'what breaks if I rename `amount`?'}\n"
        "- {class: architecture, query: 'how does checkout work?'}\n",
        encoding="utf-8",
    )
    queries = [
        *routing.extra_queries(extra),
        routing.LabelledQuery("u-1", "where is the blockchain ledger?", "unanswerable"),
    ]
    router = QueryRouter(AnthropicClient(settings(api_key=None)))  # rules only

    report = await routing.evaluate(
        router, queries, CostCalculator(DEFAULT_PRICE_TABLE), mode="rules"
    )

    assert [r.predicted for r in report.rows] == ["symbol", "impact", "conceptual", "conceptual"]
    assert report.accuracy == pytest.approx(2 / 3)
    assert report.cost_per_call() == Decimal(0)
    text = routing.render(report)
    assert "accuracy: **0.667** (2/3 answerable queries)" in text
    # Columns: the classes that occur (symbol, conceptual, architecture, impact).
    assert "| **architecture** |  | 1 |  |  | 0.00 |" in text
    assert "Unanswerable items routed to: conceptual 1" in text


def test_extra_queries_need_a_router_class(tmp_path: Path) -> None:
    extra = tmp_path / "queries.yaml"
    extra.write_text("- {class: unanswerable, query: x}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown class"):
        routing.extra_queries(extra)


async def test_a_router_run_is_recorded_like_a_retrieval_run() -> None:
    queries = [
        routing.LabelledQuery("q-1", "useCartStore", "symbol"),
        routing.LabelledQuery("q-2", "how is checkout structured?", "flow"),
        routing.LabelledQuery("u-1", "where is the blockchain ledger?", "unanswerable"),
    ]
    router = QueryRouter(AnthropicClient(settings(api_key=None)))
    report = await routing.evaluate(
        router, queries, CostCalculator(DEFAULT_PRICE_TABLE), mode="rules"
    )

    record = routing.to_record(report, sources=["fastapi-template", "q.yaml"], git_sha="abc")

    assert (record.suite, record.suite_version, record.retriever) == ("router", 1, "router-rules")
    assert record.config == {
        "sources": ["fastapi-template", "q.yaml"],
        "prompt_version": "route_query.v1",
    }
    assert record.metrics["accuracy"] == pytest.approx(0.5)
    assert record.metrics["recall"] == {"symbol": 1.0, "flow": 0.0}
    assert record.metrics["answerable"] == 2
    assert [item.metrics["correct"] for item in record.per_item] == [1.0, 0.0, None]
    assert record.per_item[1].hits == [{"routed": "architecture", "source": "fallback"}]
    assert RunRecord.model_validate_json(record.model_dump_json()) == record
