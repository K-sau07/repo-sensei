"""Impact (6.4) on ten hand-labelled changes to a small app (an API, its frontend's pages, tests
and tables): what each breaks, sorted into routes, screens, tests and tables, with precision and
recall reported; scores rank the near and the sure first; inferred and unsure edges aren't
followed."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.impact import CATEGORIES, DECAY, changed_nodes, impact

API = "backend/app/api/items.py"
AUTH = "backend/app/api/login.py"
CRUD = "backend/app/crud.py"
MODELS = "backend/app/models.py"
UTILS = "backend/app/utils.py"
SECURITY = "backend/app/security.py"
DEPS = "backend/app/deps.py"
TESTS = "backend/tests/test_items.py"
PAGE = "frontend/src/routes/items.tsx"
LIST = "frontend/src/components/ItemList.tsx"
ADD = "frontend/src/components/AddItem.tsx"
LOGIN_PAGE = "frontend/src/routes/login.tsx"
CLIENT = "frontend/src/client.ts"
HOOKS = "frontend/src/hooks.ts"

NODES: Mapping[str, tuple[NodeKind, str | None]] = {
    "GET /items": (NodeKind.API_ENDPOINT, None),
    "POST /items": (NodeKind.API_ENDPOINT, None),
    "DELETE /items": (NodeKind.API_ENDPOINT, None),
    "POST /login": (NodeKind.API_ENDPOINT, None),
    "list_items": (NodeKind.FUNCTION, API),
    "create_item_route": (NodeKind.FUNCTION, API),
    "delete_item_route": (NodeKind.FUNCTION, API),
    "login": (NodeKind.FUNCTION, AUTH),
    "get_items": (NodeKind.FUNCTION, CRUD),
    "create_item": (NodeKind.FUNCTION, CRUD),
    "delete_item": (NodeKind.FUNCTION, CRUD),
    "get_user": (NodeKind.FUNCTION, CRUD),
    "slugify": (NodeKind.FUNCTION, UTILS),
    "verify_password": (NodeKind.FUNCTION, SECURITY),
    "get_db": (NodeKind.FUNCTION, DEPS),
    "Item": (NodeKind.ORM_MODEL, MODELS),
    "User": (NodeKind.ORM_MODEL, MODELS),
    "table item": (NodeKind.TABLE, None),
    "table user": (NodeKind.TABLE, None),
    "ItemsPage": (NodeKind.COMPONENT, PAGE),
    "ItemList": (NodeKind.COMPONENT, LIST),
    "AddItem": (NodeKind.COMPONENT, ADD),
    "LoginPage": (NodeKind.COMPONENT, LOGIN_PAGE),
    "LoginForm": (NodeKind.COMPONENT, LOGIN_PAGE),
    "useItems": (NodeKind.HOOK, HOOKS),
    "listItems": (NodeKind.FUNCTION, CLIENT),
    "createItem": (NodeKind.FUNCTION, CLIENT),
    "loginUser": (NodeKind.FUNCTION, CLIENT),
    "call GET /items": (NodeKind.HTTP_CLIENT_CALL, CLIENT),
    "call POST /items": (NodeKind.HTTP_CLIENT_CALL, CLIENT),
    "call POST /login": (NodeKind.HTTP_CLIENT_CALL, CLIENT),
    "test_create_item": (NodeKind.TEST, TESTS),
    "test_get_items": (NodeKind.TEST, TESTS),
    "test_login": (NodeKind.TEST, TESTS),
}

T = EdgeType
EDGES: list[tuple[str, str, EdgeType, float, Provenance]] = [
    ("GET /items", "list_items", T.HANDLED_BY, 1.0, Provenance.RUNTIME),
    ("POST /items", "create_item_route", T.HANDLED_BY, 1.0, Provenance.RUNTIME),
    ("DELETE /items", "delete_item_route", T.HANDLED_BY, 1.0, Provenance.RUNTIME),
    ("POST /login", "login", T.HANDLED_BY, 1.0, Provenance.RUNTIME),
    ("list_items", "get_items", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("create_item_route", "create_item", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("delete_item_route", "delete_item", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("login", "verify_password", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("login", "get_user", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("list_items", "get_db", T.INJECTS, 1.0, Provenance.IMPORT_RESOLVED),
    ("create_item_route", "get_db", T.INJECTS, 1.0, Provenance.IMPORT_RESOLVED),
    ("delete_item_route", "get_db", T.INJECTS, 1.0, Provenance.IMPORT_RESOLVED),
    ("get_items", "Item", T.READS, 0.9, Provenance.IMPORT_RESOLVED),
    ("create_item", "Item", T.WRITES, 0.9, Provenance.IMPORT_RESOLVED),
    ("delete_item", "Item", T.WRITES, 0.9, Provenance.IMPORT_RESOLVED),
    ("get_user", "User", T.READS, 0.9, Provenance.IMPORT_RESOLVED),
    ("Item", "table item", T.MAPS_TO, 1.0, Provenance.CONTRACT),
    ("User", "table user", T.MAPS_TO, 1.0, Provenance.CONTRACT),
    ("create_item", "slugify", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("delete_item", "slugify", T.CALLS, 0.6, Provenance.LLM_INFERRED),  # a model's guess
    ("login", "slugify", T.CALLS, 0.3, Provenance.NAME_AMBIGUOUS),  # too unsure
    ("ItemsPage", "ItemList", T.RENDERS, 1.0, Provenance.IMPORT_RESOLVED),
    ("ItemsPage", "AddItem", T.RENDERS, 1.0, Provenance.IMPORT_RESOLVED),
    ("ItemList", "useItems", T.USES_HOOK, 1.0, Provenance.IMPORT_RESOLVED),
    ("useItems", "listItems", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("AddItem", "createItem", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("LoginPage", "LoginForm", T.RENDERS, 1.0, Provenance.IMPORT_RESOLVED),
    ("LoginForm", "loginUser", T.CALLS, 0.95, Provenance.IMPORT_RESOLVED),
    ("listItems", "call GET /items", T.ISSUES_REQUEST, 1.0, Provenance.CONTRACT),
    ("createItem", "call POST /items", T.ISSUES_REQUEST, 1.0, Provenance.CONTRACT),
    ("loginUser", "call POST /login", T.ISSUES_REQUEST, 1.0, Provenance.CONTRACT),
    ("call GET /items", "GET /items", T.TARGETS, 0.9, Provenance.URL_LITERAL),
    ("call POST /items", "POST /items", T.TARGETS, 0.9, Provenance.URL_LITERAL),
    ("call POST /login", "POST /login", T.TARGETS, 0.9, Provenance.URL_LITERAL),
    ("test_create_item", "create_item", T.TESTS, 1.0, Provenance.IMPORT_RESOLVED),
    ("test_get_items", "get_items", T.TESTS, 1.0, Provenance.IMPORT_RESOLVED),
    ("test_login", "login", T.TESTS, 1.0, Provenance.IMPORT_RESOLVED),
]


@pytest.fixture(scope="module")
def graph() -> GraphSnapshot:
    nodes = [
        Node(key, kind.value, key, key, path, 1, 9, "python") for key, (kind, path) in NODES.items()
    ]
    at = {key: i for i, key in enumerate(NODES)}
    edges = [(at[a], at[b], Edge(t, c, p, None, None)) for a, b, t, c, p in EDGES]
    return GraphSnapshot(nodes, edges)


Expected = dict[str, set[str]]

# The ten changes, and what each breaks (labelled by hand: routes, screens, tests, tables).
LABELLED: list[tuple[dict[str, list[str]], Expected]] = [
    (
        {"symbols": ["create_item"]},
        {"routes": {"POST /items"}, "screens": {"ItemsPage"}, "tests": {"test_create_item"},
         "tables": {"table item"}},
    ),
    (  # not through the inferred call, nor the unsure one
        {"symbols": ["slugify"]},
        {"routes": {"POST /items"}, "screens": {"ItemsPage"}, "tests": {"test_create_item"},
         "tables": set()},
    ),
    (
        {"symbols": ["User"]},
        {"routes": {"POST /login"}, "screens": {"LoginPage"}, "tests": {"test_login"},
         "tables": {"table user"}},
    ),
    (
        {"symbols": ["table item"]},
        {"routes": {"GET /items", "POST /items", "DELETE /items"}, "screens": {"ItemsPage"},
         "tests": {"test_create_item", "test_get_items"}, "tables": set()},
    ),
    (
        {"symbols": ["login"]},
        {"routes": {"POST /login"}, "screens": {"LoginPage"}, "tests": {"test_login"},
         "tables": set()},
    ),
    (
        {"symbols": ["verify_password"]},
        {"routes": {"POST /login"}, "screens": {"LoginPage"}, "tests": {"test_login"},
         "tables": set()},
    ),
    (  # a client function: its page, no endpoint (the endpoint doesn't depend on it)
        {"symbols": ["listItems"]},
        {"routes": set(), "screens": {"ItemsPage"}, "tests": set(), "tables": set()},
    ),
    (
        {"symbols": ["useItems"]},
        {"routes": set(), "screens": {"ItemsPage"}, "tests": set(), "tables": set()},
    ),
    (
        {"symbols": ["get_db"]},
        {"routes": {"GET /items", "POST /items", "DELETE /items"}, "screens": {"ItemsPage"},
         "tests": set(), "tables": set()},
    ),
    (  # a whole file
        {"files": [CRUD]},
        {"routes": {"GET /items", "POST /items", "DELETE /items", "POST /login"},
         "screens": {"ItemsPage", "LoginPage"}, "tests": {"test_create_item", "test_get_items",
         "test_login"}, "tables": {"table item", "table user"}},
    ),
]  # fmt: skip

LABELLED_CATEGORIES = ("routes", "screens", "tests", "tables")


def test_ten_labelled_changes_precision_and_recall(graph: GraphSnapshot) -> None:
    hits = found_total = wanted_total = 0
    for change, expected in LABELLED:
        changed = changed_nodes(
            graph, symbols=change.get("symbols", ()), files=change.get("files", ())
        )
        assert changed, change
        result = impact(graph, changed)
        for category in LABELLED_CATEGORIES:
            found = {a.node.symbol_key for a in result.affected[category]}
            wanted = expected[category]
            hits += len(found & wanted)
            found_total += len(found)
            wanted_total += len(wanted)
            assert found == wanted, (change, category, found, wanted)
    precision = hits / found_total
    recall = hits / wanted_total
    print(f"impact on 10 labelled changes: precision {precision:.2f}, recall {recall:.2f}")
    assert (precision, recall) == (1.0, 1.0)


def test_scores_rank_the_near_and_the_sure_first(graph: GraphSnapshot) -> None:
    result = impact(graph, changed_nodes(graph, symbols=["create_item"]))
    heat = result.heat
    assert heat["create_item"] == 1.0
    assert heat["create_item_route"] == pytest.approx(0.95 * DECAY, abs=1e-4)
    # An endpoint is its handler seen from outside: no hop on (as near, as sure).
    assert heat["create_item_route"] == heat["POST /items"] > heat["ItemsPage"]
    route = result.affected["routes"][0]
    assert (route.via, route.edge, route.distance) == ("create_item_route", T.HANDLED_BY, 1)
    # The client call is the endpoint seen from the page: the page's code is one hop past it,
    # only the URL's match (0.9) between.
    by_key = {a.node.symbol_key: a for items in result.affected.values() for a in items}
    assert by_key["createItem"].distance == 2
    assert heat["createItem"] == pytest.approx(heat["POST /items"] * 0.9 * DECAY, abs=1e-4)
    assert set(result.affected) == set(CATEGORIES)
    symbols = [a.node.symbol_key for a in result.affected["symbols"]]
    assert symbols[0] == "create_item_route"
    assert "slugify" not in heat  # what it calls isn't broken by it


def test_a_lower_bar_follows_unsure_edges_but_never_inferred_ones(graph: GraphSnapshot) -> None:
    low = impact(graph, changed_nodes(graph, symbols=["slugify"]), min_conf=0.2)
    routes = {a.node.symbol_key for a in low.affected["routes"]}
    assert routes == {"POST /items", "POST /login"}  # the unsure call counted now
    assert "delete_item" not in low.heat  # the inferred one never
    assert impact(graph, changed_nodes(graph, symbols=["nope"])).heat == {}
    capped = impact(graph, changed_nodes(graph, files=[CRUD]), limit=3)
    assert capped.truncated
    assert sum(len(v) for v in capped.affected.values()) == 3


def test_a_node_reached_surest_far_off_is_walked_on_from_where_it_is_reached_nearer() -> None:
    """S ← a1 ← … ← a7 ← X, every edge sure; S ← b1 ← X, a less sure way; Y → X. The surest
    way reaches X at the depth limit (8): from there nothing more. The other reaches it in 2,
    so Y, a hop on, is 3 from the change and must be found."""
    keys = ["S", *(f"a{i}" for i in range(1, 8)), "b1", "X", "Y"]
    nodes = [Node(k, NodeKind.FUNCTION.value, k, k, f"src/{k}.py", 1, 2, "python") for k in keys]
    at = {k: i for i, k in enumerate(keys)}

    def calls(src: str, dst: str, confidence: float) -> tuple[int, int, Edge]:
        return (
            at[src],
            at[dst],
            Edge(EdgeType.CALLS, confidence, Provenance.IMPORT_RESOLVED, None, None),
        )

    chain = ["S", *(f"a{i}" for i in range(1, 8)), "X"]
    edges = [calls(chain[i + 1], chain[i], 1.0) for i in range(len(chain) - 1)]
    edges += [calls("b1", "S", 0.55), calls("X", "b1", 0.55), calls("Y", "X", 1.0)]
    graph = GraphSnapshot(nodes, edges)
    found = impact(graph, changed_nodes(graph, symbols=["S"]))
    by_key = {a.node.symbol_key: a for items in found.affected.values() for a in items}
    assert by_key["X"].distance == 8  # its surest way, the one it is ranked by
    assert by_key["Y"].distance == 3  # through X's nearer way
    assert by_key["Y"].via == "X"


def test_a_route_is_as_near_as_its_handler_at_the_depth_limit() -> None:
    """S ← a1 ← … ← a7 ← H (the handler, 8 hops off), and the endpoint E handled by H. E is
    found: an endpoint is its handler's other face, no hop past it."""
    keys = ["S", *(f"a{i}" for i in range(1, 8)), "H"]
    nodes = [Node(k, NodeKind.FUNCTION.value, k, k, f"src/{k}.py", 1, 2, "python") for k in keys]
    nodes.append(Node("E", NodeKind.API_ENDPOINT.value, "POST /e", "E", "src/H.py", 1, 2, "python"))
    at = {n.symbol_key: i for i, n in enumerate(nodes)}

    def edge(src: str, dst: str, kind: EdgeType) -> tuple[int, int, Edge]:
        return at[src], at[dst], Edge(kind, 1.0, Provenance.IMPORT_RESOLVED, None, None)

    chain = [*keys]
    edges = [edge(chain[i + 1], chain[i], EdgeType.CALLS) for i in range(len(chain) - 1)]
    edges.append(edge("E", "H", EdgeType.HANDLED_BY))
    graph = GraphSnapshot(nodes, edges)
    found = impact(graph, changed_nodes(graph, symbols=["S"]))
    [route] = found.affected["routes"]
    assert (route.node.symbol_key, route.distance, route.via) == ("E", 8, "H")
    assert found.heat["E"] == found.heat["H"]
