"""Diagrams (6.1): the model labels a subgraph; the validator keeps only what the graph proves,
sends back what it doesn't, and says how much of the diagram is verified."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx2
import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_core.orgs.model_choices import StepChoice
from sensei_engine.diagrams import (
    MAX_DEPTH,
    DiagramError,
    DiagramSpec,
    Draft,
    from_entries,
    generate,
    mermaid,
    plain,
    render,
    validate,
)
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_testing.anthropic import FakeAnthropic, message


def _node(key: str, kind: NodeKind = NodeKind.FUNCTION, path: str = "app/items.py") -> Node:
    name = key.rsplit("/", maxsplit=1)[-1]
    return Node(key, kind.value, name, name, path, 10, 20, "python")


def _edge(
    kind: EdgeType = EdgeType.CALLS, provenance: Provenance = Provenance.IMPORT_RESOLVED
) -> Edge:
    return Edge(kind, 0.9, provenance, "app/items.py", 12)


def _graph(nodes: Sequence[Node], edges: Sequence[tuple[str, str, Edge]]) -> GraphSnapshot:
    at = {n.symbol_key: i for i, n in enumerate(nodes)}
    return GraphSnapshot(list(nodes), [(at[a], at[b], e) for a, b, e in edges])


ROUTE = "py app/items.py/create_item"
SERVICE = "py app/crud.py/save_item"
MODEL = "py app/models.py/Item"
OTHER = "py app/other.py/unrelated"
GUESSED = "py app/jobs.py/send_mail"


@pytest.fixture
def graph() -> GraphSnapshot:
    return _graph(
        [
            _node(ROUTE),
            _node(SERVICE),
            _node(MODEL, NodeKind.ORM_MODEL),
            _node(OTHER),
            _node(GUESSED),
        ],
        [
            (ROUTE, SERVICE, _edge()),
            (SERVICE, MODEL, _edge(EdgeType.WRITES)),
            (ROUTE, GUESSED, _edge(provenance=Provenance.LLM_INFERRED)),  # a model's guess
            (SERVICE, OTHER, _edge(EdgeType.CONTAINS)),  # structure, not flow
        ],
    )


def _draft(nodes: list[str], edges: list[tuple[str, str, str]]) -> Draft:
    return Draft.model_validate(
        {
            "title": "Creating an item",
            "nodes": [{"id": n, "label": f"step {n}"} for n in nodes],
            "edges": [
                {"source": a, "target": b, "label": "calls", "inferred_reason": why}
                for a, b, why in edges
            ],
        }
    )


def test_the_walk_follows_flow_edges_the_graph_knows(graph: GraphSnapshot) -> None:
    sub = from_entries(graph, [graph.node(ROUTE)], "how is an item created?")  # type: ignore[list-item]
    keys = [c.node.symbol_key for c in sub.nodes]
    assert keys == [ROUTE, SERVICE, MODEL]  # not the guess, not what it contains
    assert [(e.src, e.dst, e.type) for e in sub.edges] == [
        ("n1", "n2", EdgeType.CALLS),
        ("n2", "n3", EdgeType.WRITES),
    ]
    shown = render(sub, "flowchart")
    assert "n1 [" in shown
    assert "n1 -> n2 calls" in shown
    assert MAX_DEPTH == 4


def test_the_validator_rejects_invented_nodes_and_edges(graph: GraphSnapshot) -> None:
    sub = from_entries(graph, [graph.node(ROUTE)], "items")  # type: ignore[list-item]
    checked = validate(
        _draft(["n1", "n2", "n3", "n9"], [("n1", "n2", ""), ("n1", "n3", ""), ("n3", "n2", "")]),
        sub,
        graph,
        "flowchart",
    )
    assert any("'n9' isn't one of the numbered nodes" in p for p in checked.problems)
    assert any("n1 -> n3 isn't in the graph" in p for p in checked.problems)
    assert any("n3 -> n2 is the other way" in p for p in checked.problems)
    assert [(e.src, e.dst, e.verified) for e in checked.spec.edges] == [(ROUTE, SERVICE, True)]


def test_an_inferred_edge_says_why_and_counts_against_the_ratio(graph: GraphSnapshot) -> None:
    sub = from_entries(graph, [graph.node(ROUTE)], "items")  # type: ignore[list-item]
    checked = validate(
        _draft(
            ["n1", "n2", "n3"],
            [("n1", "n2", ""), ("n2", "n3", ""), ("n3", "n1", "the ORM calls back on commit")],
        ),
        sub,
        graph,
        "flowchart",
    )
    assert checked.problems == []
    spec = checked.spec
    assert [e.verified for e in spec.edges] == [True, True, False]
    assert spec.edges[2].inferred == "the ORM calls back on commit"
    assert spec.verified_ratio == pytest.approx(2 / 3)
    # Citations are the graph's, not the model's.
    assert spec.nodes[0].as_json()["citation"] == {
        "path": "app/items.py", "start_line": 10, "end_line": 20,
    }  # fmt: skip
    assert DiagramSpec.from_json(spec.as_json()).as_json() == spec.as_json()


def test_the_ratio_math() -> None:
    assert DiagramSpec("t", "flowchart", []).verified_ratio == 1.0  # nothing to prove


def _answers(*drafts: dict[str, Any]) -> tuple[FakeAnthropic, list[dict[str, Any]]]:
    queue = list(drafts)
    sent: list[dict[str, Any]] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=message(json.dumps(queue.pop(0))))

    return FakeAnthropic(routes={"/v1/messages": reply}), sent


async def test_a_wrong_answer_is_sent_back_with_what_was_wrong(graph: GraphSnapshot) -> None:
    sub = from_entries(graph, [graph.node(ROUTE)], "items")  # type: ignore[list-item]
    wrong = _draft(["n1", "n2", "n7"], [("n1", "n2", ""), ("n2", "n7", "")]).model_dump()
    right = _draft(["n1", "n2", "n3"], [("n1", "n2", ""), ("n2", "n3", "")]).model_dump()
    fake, sent = _answers(wrong, right)
    made = await generate(fake.client(), sub, graph, "sequence")
    assert made.tries == 2
    assert made.problems == []
    assert made.spec.verified_ratio == 1.0
    feedback = sent[1]["messages"][0]["content"]
    assert "Your last answer had these problems" in feedback
    assert "'n7' isn't one of the numbered nodes" in feedback


async def test_the_labels_run_on_haiku_unless_the_org_raised_them(graph: GraphSnapshot) -> None:
    sub = from_entries(graph, [graph.node(ROUTE)], "items")  # type: ignore[list-item]
    right = _draft(["n1", "n2", "n3"], [("n1", "n2", ""), ("n2", "n3", "")]).model_dump()
    fake, sent = _answers(right, right)
    await generate(fake.client(), sub, graph, "sequence")
    await generate(
        fake.client(), sub, graph, "sequence", floor=StepChoice(tier="standard", effort="medium")
    )
    assert [(b["model"], b["output_config"]["effort"]) for b in sent] == [
        ("claude-haiku-5-5", "low"), ("claude-sonnet-5-5", "medium"),
    ]  # fmt: skip


async def test_after_the_retries_what_is_wrong_is_left_out(graph: GraphSnapshot) -> None:
    sub = from_entries(graph, [graph.node(ROUTE)], "items")  # type: ignore[list-item]
    wrong = _draft(["n1", "n2", "n3"], [("n1", "n2", ""), ("n1", "n3", "")]).model_dump()
    fake, sent = _answers(wrong, wrong, wrong)
    made = await generate(fake.client(), sub, graph, "flowchart")
    assert (made.tries, len(sent)) == (3, 3)  # the first answer and two retries
    assert [(e.src, e.dst) for e in made.spec.edges] == [(ROUTE, SERVICE)]
    assert made.problems  # said, for the log


async def test_without_a_model_the_subgraph_is_drawn_as_it_is(graph: GraphSnapshot) -> None:
    sub = from_entries(graph, [graph.node(ROUTE)], "items")  # type: ignore[list-item]
    made = await generate(None, sub, graph, "flowchart")
    assert made.tries == 0
    assert [n.label for n in made.spec.nodes] == ["create_item", "save_item", "Item"]
    assert made.spec.verified_ratio == 1.0
    assert plain(sub, "flowchart", graph).as_json() == made.spec.as_json()
    with pytest.raises(DiagramError):
        from_entries(graph, [], "nothing")


def test_mermaid_is_compiled_from_the_spec(graph: GraphSnapshot) -> None:
    sub = from_entries(graph, [graph.node(ROUTE)], "items")  # type: ignore[list-item]
    spec = validate(
        _draft(["n1", "n2", "n3"], [("n1", "n2", ""), ("n3", "n1", 'a "hook" [x]')]),
        sub,
        graph,
        "flowchart",
    ).spec
    flow = mermaid(spec, link=lambda node: f"/acme/api/code/{node.path}?L={node.start_line}")
    assert flow.splitlines()[0] == "flowchart LR"
    assert 'n1["step n1"]' in flow
    assert "n1 -->|calls| n2" in flow
    assert "n3 -.->|calls| n1" in flow  # inferred: dashed
    assert "linkStyle 1 stroke-dasharray:4 3" in flow
    assert 'click n1 href "/acme/api/code/app/items.py?L=10"' in flow
    spec.kind = "sequence"
    sequence = mermaid(spec)
    assert sequence.splitlines()[0] == "sequenceDiagram"
    assert "n1->>n2: calls" in sequence
    assert "n3-->>n1: calls" in sequence
    spec.kind = "architecture"
    assert "subgraph other" in mermaid(spec)
    # Labels can't break out of their quotes.
    bad = validate(_draft(["n1"], []), sub, graph, "flowchart").spec
    bad.nodes[0] = type(bad.nodes[0])(ROUTE, 'x"]; click n1 "evil', "api", "function", None, 1, 1)
    drawn = mermaid(bad)
    assert drawn.splitlines()[1] == '  n1["x    click n1  evil"]'
