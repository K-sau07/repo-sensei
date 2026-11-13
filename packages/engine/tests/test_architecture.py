"""The whole app's architecture: its modules, their lines and the outside systems from the
graph; the parts grouped and named by the model, checked, the rest drawn by folder; every line
but the user's the graph's."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import httpx2
import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.architecture import (
    Component,
    Facts,
    Line,
    Part,
    check,
    draw,
    facts_of,
    plain,
    spec_of,
    whole_app,
)
from sensei_engine.architecture import Draft as ArchitectureDraft
from sensei_engine.deps import grouping
from sensei_engine.diagrams import DiagramSpec
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_testing.anthropic import FakeAnthropic, message

ROUTE = "py backend/app/api/items.py/create"
CRUD = "py backend/app/crud.py/save"
MODEL = "py backend/app/models.py/Item"
PAGE = "ts frontend/src/pages/Items.tsx/Items"
CALL = "http_client_call POST /items frontend/src/pages/Items.tsx:9"
ENDPOINT = "api_endpoint POST /items"
TABLE = "table item"
DRIVER = "package py:psycopg"
STRIPE = "package npm:stripe"
FILES = [
    "backend/pyproject.toml", "backend/app/api/items.py", "backend/app/crud.py",
    "backend/app/models.py", "frontend/package.json", "frontend/src/pages/Items.tsx",
]  # fmt: skip
FRAMEWORKS = [
    {"framework": "fastapi", "root": "backend"},
    {"framework": "react", "root": "frontend"},
]


def _node(key: str, path: str | None, kind: NodeKind = NodeKind.FUNCTION) -> Node:
    name = key.rsplit("/", maxsplit=1)[-1]
    return Node(key, kind.value, name, name, path, 1, 5, "python")


def _edge(kind: EdgeType = EdgeType.CALLS, path: str = "backend/app/api/items.py") -> Edge:
    return Edge(kind, 0.9, Provenance.IMPORT_RESOLVED, path, 3)


@pytest.fixture
def graph() -> GraphSnapshot:
    nodes = [
        _node(ROUTE, "backend/app/api/items.py"),
        _node(CRUD, "backend/app/crud.py"),
        _node(MODEL, "backend/app/models.py", NodeKind.ORM_MODEL),
        _node(PAGE, "frontend/src/pages/Items.tsx", NodeKind.COMPONENT),
        _node(CALL, "frontend/src/pages/Items.tsx", NodeKind.HTTP_CLIENT_CALL),
        _node(ENDPOINT, None, NodeKind.API_ENDPOINT),
        _node(TABLE, None, NodeKind.TABLE),
        _node(DRIVER, None, NodeKind.PACKAGE),
        _node(STRIPE, None, NodeKind.PACKAGE),
    ]
    at = {n.symbol_key: i for i, n in enumerate(nodes)}
    edges = [
        (ROUTE, CRUD, _edge()),
        (ROUTE, CRUD, _edge()),
        (CRUD, MODEL, _edge(EdgeType.WRITES, "backend/app/crud.py")),
        (CRUD, DRIVER, _edge(EdgeType.IMPORTS, "backend/app/crud.py")),
        (PAGE, STRIPE, _edge(EdgeType.IMPORTS, "frontend/src/pages/Items.tsx")),
        (PAGE, CALL, _edge(EdgeType.ISSUES_REQUEST, "frontend/src/pages/Items.tsx")),
        (CALL, ENDPOINT, _edge(EdgeType.TARGETS, "frontend/src/pages/Items.tsx")),
        (ENDPOINT, ROUTE, _edge(EdgeType.HANDLED_BY)),
        (MODEL, TABLE, _edge(EdgeType.MAPS_TO, "backend/app/models.py")),
    ]
    return GraphSnapshot(nodes, [(at[a], at[b], e) for a, b, e in edges])


@pytest.fixture
def facts(graph: GraphSnapshot) -> Facts:
    return facts_of(graph, grouping(FILES, FRAMEWORKS))


def test_the_facts_are_the_graphs(facts: Facts) -> None:
    tiers = {part.id: part.tier for part in facts.parts}
    assert tiers["frontend/src/pages"] == "frontend"
    assert tiers["backend/app/api"] == "api"
    page = next(p for p in facts.parts if p.id == "frontend/src/pages")
    assert [(m.name, m.kind) for m in page.members] == [("Items", "component")]
    # The outside systems, known by their packages, and who imports them.
    outside = {o.name: (o.kind, dict(o.users)) for o in facts.outside}
    assert outside == {
        "PostgreSQL": ("database", {"backend/app": 1}),
        "Stripe": ("payments", {"frontend/src/pages": 1}),
    }
    assert dict(facts.data_users) == {"backend/app": 1}  # crud.py writes the model


def test_without_a_model_a_part_per_module_and_the_lines_between(graph: GraphSnapshot) -> None:
    spec = whole_app(graph, grouping(FILES, FRAMEWORKS), "Architecture of o/items")
    assert spec.kind == "architecture"
    by_id = {n.id: n for n in spec.nodes}
    assert by_id["actor:user"].label == "User"
    assert by_id["outside:postgresql"].kind == "database"
    assert {n.label for n in spec.nodes if n.layer == "frontend"} == {"Pages"}
    # The user's line is inferred, and says why; every other one is the graph's.
    user = [e for e in spec.edges if e.src == "actor:user"]
    assert [(e.verified, e.inferred) for e in user] == [
        (False, "people use the app through its pages")
    ]
    rest = [e for e in spec.edges if e.src != "actor:user"]
    assert rest
    assert all(e.verified and e.provenance == "graph" for e in rest)
    lines = {(by_id[e.src].label, by_id[e.dst].label, e.label) for e in rest}
    assert ("Pages", "API", "HTTP requests") in lines
    assert ("API", "App", "2 calls") in lines
    assert ("App", "PostgreSQL", "1 reads and writes") in lines  # the one database: the tables'
    assert ("Pages", "Stripe", "imports") in lines
    assert DiagramSpec.from_json(spec.as_json()) == spec


def _answers(*drafts: dict[str, Any]) -> tuple[FakeAnthropic, list[dict[str, Any]]]:
    queue = list(drafts)
    sent: list[dict[str, Any]] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=message(json.dumps(queue.pop(0))))

    return FakeAnthropic(routes={"/v1/messages": reply}), sent


def _component(name: str, role: str, modules: list[str], icon: str = "server") -> dict[str, Any]:
    return {
        "name": name,
        "role": role,
        "icon": icon,
        "description": f"The {name}.",
        "modules": modules,
    }


async def test_the_model_names_the_parts_and_what_it_gets_wrong_is_sent_back(
    facts: Facts,
) -> None:
    wrong = {
        "name": "Items", "summary": "Keeps a list of items.",
        "components": [
            _component("Web app", "frontend", ["frontend/src/pages", "frontend/src/no"], "monitor"),
            _component("Items API", "api", ["backend/app/api"]),
        ],
    }  # fmt: skip
    right = {
        "name": "Items", "summary": "Keeps a list of items.",
        "components": [
            _component("Web app", "frontend", ["frontend/src/pages"], "monitor"),
            _component("Items API", "api", ["backend/app/api"]),
            _component("Data models", "data", ["backend/app"], "database"),
        ],
    }  # fmt: skip
    flow = {
        "lines": [
            {"from": "part:web-app", "to": "part:items-api", "label": "posts new items"},
            {"from": "part:web-app", "to": "part:nowhere", "label": "invented"},
        ],
        "steps": [
            {"from": "actor:user", "to": "part:web-app", "text": "A user opens the items page."},
            {"from": "part:web-app", "to": "part:items-api", "text": "It posts the new item."},
            {"from": "part:items-api", "to": "part:web-app", "text": "Backwards: not a line."},
        ],
    }
    fake, sent = _answers(wrong, right, flow)
    made = await draw(fake.client(), facts, "Architecture of o/items", "o/items")
    assert (made.tries, made.problems) == (2, [])
    # The model said what the lines do, and walked through them: only the diagram's own.
    said = {(e.src, e.dst): e.label for e in made.spec.edges}
    assert said[("part:web-app", "part:items-api")] == "posts new items"
    assert [(x.src, x.dst) for x in made.spec.steps] == [
        ("actor:user", "part:web-app"), ("part:web-app", "part:items-api"),
    ]  # fmt: skip
    assert "create → save" in sent[2]["messages"][0]["content"]  # the code behind lines
    feedback = sent[1]["messages"][0]["content"]
    assert "'frontend/src/no' is not one of the module ids" in feedback
    assert "these modules are in no component: backend/app" in feedback
    spec = made.spec
    assert spec.summary == "Keeps a list of items."
    parts = {n.label: n for n in spec.nodes}
    assert (parts["Web app"].layer, parts["Web app"].icon, parts["Web app"].note) == (
        "frontend", "monitor", "The Web app.",
    )  # fmt: skip
    held = [(m.name, m.kind) for m in parts["Data models"].members]
    assert held[0] == ("backend/app", "module")  # its folders, then what they hold
    assert ("Item", "model") in held
    assert ("save", "function") in held  # its code joined to other modules'
    # A line keeps the code's own edges behind it.
    api_line = next(e for e in spec.edges if e.src == parts["Items API"].id and e.count)
    assert api_line.count == 2
    assert [(x.src, x.dst) for x in api_line.samples] == [("create", "save")]
    lines = {(parts_of(spec, e.src), parts_of(spec, e.dst)) for e in spec.edges}
    assert ("Web app", "Items API") in lines
    assert ("Items API", "Data models") in lines
    assert ("Data models", "PostgreSQL") in lines


def parts_of(spec: DiagramSpec, key: str) -> str:
    return next(n.label for n in spec.nodes if n.id == key)


async def test_what_the_model_leaves_out_is_drawn_by_its_folder(facts: Facts) -> None:
    partial = {
        "name": "Items", "summary": "",
        "components": [_component("Web app", "frontend", ["frontend/src/pages"], "monitor")],
    }  # fmt: skip
    fake, _ = _answers(partial, partial, {"lines": [], "steps": []})
    made = await draw(fake.client(), facts, "t", "o/items")
    labels = {n.label for n in made.spec.nodes if n.layer not in ("actor", "outside")}
    assert labels == {"Web app", "API", "App"}  # the model's, then by folder
    assert made.problems  # it never placed them


def test_a_component_named_as_a_path_is_refused(facts: Facts) -> None:
    draft = ArchitectureDraft.model_validate({
        "name": "x", "summary": "",
        "components": [_component("backend/app", "data", ["backend/app"])],
    })  # fmt: skip
    found, problems = check(draft, facts)
    assert found == []
    assert any("not a path" in p for p in problems)


def test_without_parts_nothing_is_drawn() -> None:
    empty = Facts([], [], [])
    assert spec_of(empty, plain(empty), "t", None).nodes == []


def test_the_user_reaches_the_frontends_entries_not_every_part() -> None:
    pages = Part("web/pages", "web/pages", "frontend", 10, (), 0)
    widgets = Part("web/widgets", "web/widgets", "frontend", 30, (), 0)
    facts = Facts([pages, widgets], [Line("web/pages", "web/widgets", Counter(renders=4), 0.9)], [])
    spec = spec_of(facts, plain(facts), "t", None)
    reach = [e.dst for e in spec.edges if e.src == "actor:user"]
    assert reach == [next(n.id for n in spec.nodes if n.label == "Pages")]  # not the widgets


def test_a_frontend_folder_named_api_is_the_frontends() -> None:
    nodes = [
        Node("ts web/src/pages/api/Page.tsx/Page", NodeKind.COMPONENT.value, "Page", "Page",
             "web/src/pages/api/Page.tsx", 1, 5, "typescript"),
        *[
            Node(f"ts web/src/pages/api/x.ts/v{i}", NodeKind.VARIABLE.value, f"v{i}", f"v{i}",
                 "web/src/pages/api/x.ts", 1, 1, "typescript")
            for i in range(3)
        ],
    ]  # fmt: skip
    graph = GraphSnapshot(nodes, [])
    facts = facts_of(graph, grouping(["web/package.json"], [{"framework": "react", "root": "web"}]))
    assert [(p.id, p.tier) for p in facts.parts] == [("web/src/pages/api", "frontend")]


def test_tooling_is_left_out_of_an_app_that_has_more(facts: Facts) -> None:
    docs = Component("Javadoc scripts", "other", "file", None, ("backend/app",))
    web = Component("Web app", "frontend", "monitor", None, ("frontend/src/pages",))
    labels = {n.label for n in spec_of(facts, [web, docs], "t", None).nodes}
    assert "Javadoc scripts" not in labels
    assert "Web app" in labels
    # Nothing but tooling: it is drawn.
    assert "Javadoc scripts" in {n.label for n in spec_of(facts, [docs], "t", None).nodes}
