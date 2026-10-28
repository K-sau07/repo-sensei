"""``trace_feature`` (3.12): on the fastapi-template, linked as the graph and links stages link it,
the full path from a page to the table it reads; then, on small graphs, what makes a path win,
where one breaks and why, ambiguous links, overrides, nested functions and the entry's forms."""

from __future__ import annotations

import tarfile
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node, lane
from sensei_engine.graph.stage import RESOLVERS, Resolver, resolve_graph, resolver_for
from sensei_engine.graph.trace import (
    MAX_DEPTH,
    TraceError,
    TracePath,
    resolve_entry,
    table_model,
    trace_feature,
)
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.ingest.languages import detect_language
from sensei_engine.link import http_calls
from sensei_engine.link.stage import (
    LinkRequest,
    env_files,
    extract_request,
    links_language,
    resolve_links,
)
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"


# ---------------------------------------------------------------- the template, linked


def _graph_of(
    project: Project,
    languages: dict[str, str],
    build_nodes: Sequence[GraphNode],
    build_edges: Sequence[GraphEdge],
) -> GraphSnapshot:
    nodes: dict[str, Node] = {}
    for path, found in project.definitions.items():
        for d in found:
            nodes[d.symbol_key] = Node(
                d.symbol_key, d.kind, d.name, d.qualified_name, path, d.start_line, d.end_line,
                languages.get(path, ""), lane(d.kind, path, None),
            )  # fmt: skip
    for n in build_nodes:
        nodes.setdefault(
            n.key,
            Node(
                n.key, n.kind.value, n.name, n.qualified_name or n.name, n.path, n.start_line,
                n.end_line, n.lang, lane(n.kind.value, n.path, None),
            ),
        )  # fmt: skip
    order = sorted(nodes)
    at = {key: i for i, key in enumerate(order)}
    edges = [
        (
            at[e.src],
            at[e.dst],
            Edge(
                e.type,
                e.confidence if e.confidence is not None else confidence_of(e.provenance),
                e.provenance,
                e.evidence_path,
                e.evidence_line,
            ),
        )
        for e in build_edges
        if e.src in at and e.dst in at
    ]
    return GraphSnapshot([nodes[key] for key in order], edges)


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> GraphSnapshot:
    """The template's graph: its code layer from the resolvers, its links from the links stage
    (static routes: no sandbox), as the two stages write them."""
    base = tmp_path_factory.mktemp("trace")
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(base, filter="data")
    root = base / "fastapi-template"
    paths = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and "node_modules" not in p.parts
    )
    languages = {
        path: language
        for path in paths
        if (language := detect_language(path, (root / path).read_bytes()[:256])) is not None
    }
    texts = {
        p: (root / p).read_text(errors="replace")
        for p in paths
        if is_config(p) or p in env_files(paths)
    }
    envs = http_calls.env_values(Project(frozenset()), {p: texts[p] for p in env_files(paths)})
    definitions: dict[str, tuple[Definition, ...]] = {}
    facts: dict[str, dict[str, object]] = {}
    for path, language in languages.items():
        name = resolver_for(language)
        if name is None and not links_language(language):
            continue
        parsed = parse_file(path, (root / path).read_bytes(), language=language)
        if not parsed.ok:
            continue
        definitions[path] = tuple(
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, language)
        )
        if name is not None:
            found = cast("Resolver", RESOLVERS[name]).extract(parsed)
            if found is not None:
                facts.setdefault(name, {})[path] = found
    project = Project(
        frozenset(paths), {p: t for p, t in texts.items() if is_config(p)}, definitions
    )
    code = resolve_graph(project, facts, clock=lambda: 0.0)
    links = resolve_links(
        project,
        {
            path: extract_request(
                LinkRequest(
                    str(root),
                    path,
                    language,
                    http_calls.env_for(path, envs) if language != "python" else {},
                )
            )
            for path, language in languages.items()
            if links_language(language)
        },
    )
    return _graph_of(project, languages, [*code.nodes, *links.nodes], [*code.edges, *links.edges])


def _route(path: TracePath) -> list[tuple[str, str]]:
    return [(hop.layer, hop.node.qualified_name) for hop in path.hops]


def test_the_login_page_traces_to_the_user_table(template: GraphSnapshot) -> None:
    """Login → its hook → the generated client → the endpoint → the handler → crud → user: the
    code says each hop (checked by hand against the template at b1125d8)."""
    result = trace_feature(template, "frontend/src/routes/login.tsx")
    assert [n.symbol_key for n in result.entries] == ["tsx frontend/src/routes/login.tsx/Login()."]
    best = result.paths[0]
    assert best.reaches_goal
    assert best.breaks == []
    assert [hop.node.symbol_key for hop in best.hops] == [
        "tsx frontend/src/routes/login.tsx/Login().",
        "ts frontend/src/hooks/useAuth.ts/useAuth().",
        "ts frontend/src/client/sdk.gen.ts/LoginService#loginAccessToken().",
        "http_client_call POST /api/v1/login/access-token frontend/src/client/sdk.gen.ts:28",
        "api_endpoint POST /api/v1/login/access-token",
        "py backend/app/api/routes/login.py/login_access_token().",
        "py backend/app/crud.py/authenticate().",
        "py backend/app/crud.py/get_user_by_email().",
        "table user",
    ]
    layers = [hop.layer for hop in best.hops]
    assert layers[:2] == ["ui", "state"]
    assert layers[3:6] == ["client", "route", "handler"]
    assert layers[-1] == "db"
    # Each hop is proven where the code says it: the handler's call on login.py:30.
    authenticate = best.hops[6]
    edge = authenticate.edge
    assert edge is not None
    assert (edge.type, edge.evidence_path, edge.evidence_line) == (
        EdgeType.CALLS, "backend/app/api/routes/login.py", 30,
    )  # fmt: skip
    assert best.confidence == pytest.approx(0.9**7)
    model = table_model(template, best.hops[-1].node)
    assert model is not None
    assert (model.qualified_name, model.path, model.start_line) == (
        "User",
        "backend/app/models.py",
        51,
    )


def test_a_route_traces_from_its_endpoint_and_a_name_finds_its_definition(
    template: GraphSnapshot,
) -> None:
    by_route = trace_feature(template, "POST /api/v1/items/")
    assert [n.symbol_key for n in by_route.entries] == ["api_endpoint POST /api/v1/items"]
    assert _route(by_route.paths[0]) == [
        ("route", "POST /api/v1/items/"),
        ("handler", "create_item"),
        ("db", "item"),
    ]
    assert by_route.paths[0].hops[-1].edge_type is EdgeType.WRITES
    by_name = trace_feature(template, "AddItem")
    assert by_name.entries[0].path == "frontend/src/components/Items/AddItem.tsx"
    assert _route(by_name.paths[0])[-1] == ("db", "item")


def test_a_trace_to_a_given_symbol_stops_there(template: GraphSnapshot) -> None:
    result = trace_feature(
        template, "frontend/src/routes/login.tsx", to="py backend/app/crud.py/authenticate()."
    )
    assert result.goal is not None
    best = result.paths[0]
    assert best.reaches_goal
    assert best.hops[-1].node.qualified_name == "authenticate"
    assert all(hop.node.kind != NodeKind.TABLE for hop in best.hops)


# ---------------------------------------------------------------- small graphs


def _node(key: str, kind: NodeKind, *, path: str | None = "a.py", lane_: str = "other") -> Node:
    name = key.rsplit("/", 1)[-1].strip("().#") or key
    return Node(key, kind.value, name, name, path, 1, 2, "python", lane_)


def _edge(kind: EdgeType, confidence: float = 0.9, line: int | None = 1) -> Edge:
    return Edge(kind, confidence, Provenance.IMPORT_RESOLVED, "a.py" if line else None, line)


def _graph(nodes: Sequence[Node], edges: Sequence[tuple[str, str, Edge]]) -> GraphSnapshot:
    at = {node.symbol_key: i for i, node in enumerate(nodes)}
    return GraphSnapshot(list(nodes), [(at[a], at[b], e) for a, b, e in edges])


PAGE = _node("ts ui.tsx/Page().", NodeKind.COMPONENT, path="ui.tsx", lane_="ui")
CALL = _node("http_client_call GET /x ui.tsx:3", NodeKind.HTTP_CLIENT_CALL, path="ui.tsx")
ENDPOINT = _node("api_endpoint GET /x", NodeKind.API_ENDPOINT)
OTHER_ENDPOINT = _node("api_endpoint GET /{}", NodeKind.API_ENDPOINT)
HANDLER = _node("py a.py/read_x().", NodeKind.FUNCTION, lane_="api")
TABLE = _node("table x", NodeKind.TABLE, path=None)


def test_a_client_call_no_endpoint_matches_is_a_break() -> None:
    graph = _graph(
        [PAGE, CALL], [(PAGE.symbol_key, CALL.symbol_key, _edge(EdgeType.ISSUES_REQUEST))]
    )
    [path] = trace_feature(graph, PAGE.symbol_key).paths
    assert not path.reaches_goal
    assert [(b.after_hop, b.reason, b.node) for b in path.breaks] == [
        (1, "client call has no matching endpoint", CALL.symbol_key)
    ]


def test_an_endpoint_without_a_handler_and_code_that_goes_nowhere_are_breaks() -> None:
    graph = _graph(
        [PAGE, CALL, ENDPOINT],
        [
            (PAGE.symbol_key, CALL.symbol_key, _edge(EdgeType.ISSUES_REQUEST)),
            (CALL.symbol_key, ENDPOINT.symbol_key, _edge(EdgeType.TARGETS)),
        ],
    )
    [path] = trace_feature(graph, PAGE.symbol_key).paths
    assert path.breaks[0].reason == "endpoint has no handler in the repository"
    lonely = _graph([HANDLER], [])
    [alone] = trace_feature(lonely, HANDLER.symbol_key).paths
    assert alone.breaks[0].reason == "read_x calls nothing the trace follows further"


def test_an_ambiguous_call_gives_a_path_per_endpoint_with_its_share() -> None:
    """A call matching two endpoints (3.10 splits the confidence) is two paths, each hop saying
    it had an alternative; the one that reaches a table ranks first."""
    graph = _graph(
        [PAGE, CALL, ENDPOINT, OTHER_ENDPOINT, HANDLER, TABLE],
        [
            (PAGE.symbol_key, CALL.symbol_key, _edge(EdgeType.ISSUES_REQUEST, 1.0)),
            (CALL.symbol_key, ENDPOINT.symbol_key, _edge(EdgeType.TARGETS, 0.25)),
            (CALL.symbol_key, OTHER_ENDPOINT.symbol_key, _edge(EdgeType.TARGETS, 0.25)),
            (ENDPOINT.symbol_key, HANDLER.symbol_key, _edge(EdgeType.HANDLED_BY, 1.0)),
            (HANDLER.symbol_key, TABLE.symbol_key, _edge(EdgeType.READS)),
        ],
    )
    first, second = trace_feature(graph, PAGE.symbol_key).paths
    assert first.reaches_goal
    assert [h.node.symbol_key for h in first.hops][-3:] == [
        ENDPOINT.symbol_key, HANDLER.symbol_key, TABLE.symbol_key,
    ]  # fmt: skip
    assert first.hops[2].alternatives == 2
    assert first.confidence == pytest.approx(1.0 * 0.25 * 1.0 * 0.9)
    assert second.hops[-1].node.symbol_key == OTHER_ENDPOINT.symbol_key
    assert second.breaks[0].reason == "endpoint has no handler in the repository"


def test_min_conf_drops_what_is_less_sure() -> None:
    graph = _graph(
        [HANDLER, TABLE], [(HANDLER.symbol_key, TABLE.symbol_key, _edge(EdgeType.READS, 0.4))]
    )
    assert trace_feature(graph, HANDLER.symbol_key).paths[0].reaches_goal
    assert not trace_feature(graph, HANDLER.symbol_key, min_conf=0.5).paths[0].reaches_goal


def test_an_interface_method_steps_to_its_implementation() -> None:
    """``Service.save`` does nothing the trace follows (abstract): the subclass's ``save`` does."""
    base = _node("py s.py/Service#", NodeKind.CLASS)
    abstract = _node("py s.py/Service#save().", NodeKind.METHOD)
    impl_class = _node("py s.py/DbService#", NodeKind.CLASS)
    impl = _node("py s.py/DbService#save().", NodeKind.METHOD)
    graph = _graph(
        [HANDLER, base, abstract, impl_class, impl, TABLE],
        [
            (HANDLER.symbol_key, abstract.symbol_key, _edge(EdgeType.CALLS)),
            (impl_class.symbol_key, base.symbol_key, _edge(EdgeType.INHERITS, 0.9)),
            (impl.symbol_key, TABLE.symbol_key, _edge(EdgeType.WRITES)),
        ],
    )
    best = trace_feature(graph, HANDLER.symbol_key).paths[0]
    assert [h.node.symbol_key for h in best.hops] == [
        HANDLER.symbol_key, abstract.symbol_key, impl.symbol_key, TABLE.symbol_key,
    ]  # fmt: skip
    assert best.hops[2].edge_type is EdgeType.IMPLEMENTS


def test_a_functions_nested_functions_edges_are_its_own() -> None:
    """A hook's callback (``useAuth().login()``) calls the client: the hook reaches it."""
    hook = _node("ts h.ts/useAuth().", NodeKind.HOOK, path="h.ts")
    inner = _node("ts h.ts/useAuth().login().", NodeKind.FUNCTION, path="h.ts")
    graph = _graph(
        [PAGE, hook, inner, CALL],
        [
            (PAGE.symbol_key, hook.symbol_key, _edge(EdgeType.USES_HOOK)),
            (inner.symbol_key, CALL.symbol_key, _edge(EdgeType.ISSUES_REQUEST, 1.0, line=9)),
        ],
    )
    best = trace_feature(graph, PAGE.symbol_key).paths[0]
    assert [h.node.symbol_key for h in best.hops] == [
        PAGE.symbol_key, hook.symbol_key, CALL.symbol_key,
    ]  # fmt: skip
    edge = best.hops[2].edge
    assert edge is not None
    assert edge.evidence_line == 9


def test_a_handler_reading_its_table_beats_a_detour_for_another_layer() -> None:
    """A handler that reads its table itself is surer than a path on through a helper (the
    service layer) that writes another table, an audit row's: more layers don't make it the
    feature's path."""
    audit = _node("py a.py/stage_audit_event().", NodeKind.FUNCTION, lane_="svc")
    audit_table = _node("table audit_events", NodeKind.TABLE, path=None)
    graph = _graph(
        [ENDPOINT, HANDLER, TABLE, audit, audit_table],
        [
            (ENDPOINT.symbol_key, HANDLER.symbol_key, _edge(EdgeType.HANDLED_BY, 1.0)),
            (HANDLER.symbol_key, TABLE.symbol_key, _edge(EdgeType.READS)),
            (HANDLER.symbol_key, audit.symbol_key, _edge(EdgeType.CALLS)),
            (audit.symbol_key, audit_table.symbol_key, _edge(EdgeType.WRITES)),
        ],
    )
    paths = trace_feature(graph, ENDPOINT.symbol_key).paths
    assert [h.node.symbol_key for h in paths[0].hops] == [
        ENDPOINT.symbol_key, HANDLER.symbol_key, TABLE.symbol_key,
    ]  # fmt: skip
    assert paths[1].hops[-1].node.symbol_key == audit_table.symbol_key


def test_a_function_is_as_near_the_table_as_its_nested_functions() -> None:
    """The way on through a hook whose callback reaches the table is tried before surer ones
    that lead nowhere, though only the callback has the edge: more of those than the fan-out
    would otherwise crowd it out."""
    hook = _node("ts h.ts/useItems().", NodeKind.HOOK, path="h.ts")
    inner = _node("ts h.ts/useItems().load().", NodeKind.FUNCTION, path="h.ts")
    noise = [_node(f"ts n.ts/useNoise{i}().", NodeKind.HOOK, path="n.ts") for i in range(8)]
    graph = _graph(
        [PAGE, hook, inner, CALL, ENDPOINT, HANDLER, TABLE, *noise],
        [
            *((PAGE.symbol_key, n.symbol_key, _edge(EdgeType.USES_HOOK, 1.0)) for n in noise),
            (PAGE.symbol_key, hook.symbol_key, _edge(EdgeType.USES_HOOK, 0.9)),
            (inner.symbol_key, CALL.symbol_key, _edge(EdgeType.ISSUES_REQUEST, 1.0)),
            (CALL.symbol_key, ENDPOINT.symbol_key, _edge(EdgeType.TARGETS, 1.0)),
            (ENDPOINT.symbol_key, HANDLER.symbol_key, _edge(EdgeType.HANDLED_BY, 1.0)),
            (HANDLER.symbol_key, TABLE.symbol_key, _edge(EdgeType.READS, 1.0)),
        ],
    )
    best = trace_feature(graph, PAGE.symbol_key).paths[0]
    assert best.reaches_goal
    assert [h.node.symbol_key for h in best.hops][:2] == [PAGE.symbol_key, hook.symbol_key]


def test_externals_are_never_on_a_path_and_depth_is_bounded() -> None:
    chain = [_node(f"py a.py/f{i}().", NodeKind.FUNCTION) for i in range(MAX_DEPTH + 3)]
    library = _node("external_symbol py:os/path", NodeKind.EXTERNAL_SYMBOL, path=None)
    edges = [(a.symbol_key, b.symbol_key, _edge(EdgeType.CALLS)) for a, b in pairwise(chain)]
    edges.append((chain[0].symbol_key, library.symbol_key, _edge(EdgeType.CALLS, 1.0)))
    paths = trace_feature(_graph([*chain, library], edges), chain[0].symbol_key).paths
    assert all(h.node.kind != NodeKind.EXTERNAL_SYMBOL for p in paths for h in p.hops)
    assert len(paths[0].hops) == MAX_DEPTH + 1
    assert paths[0].breaks[0].reason == f"the trace stops at {MAX_DEPTH} hops"


def test_an_entry_is_a_key_a_route_a_file_or_a_name() -> None:
    graph = _graph([PAGE, ENDPOINT, HANDLER], [])
    assert resolve_entry(graph, HANDLER.symbol_key) == [HANDLER]
    assert resolve_entry(graph, "get /x/") == [ENDPOINT]
    assert resolve_entry(graph, "ui.tsx") == [PAGE]
    assert resolve_entry(graph, "read_x") == [HANDLER]
    with pytest.raises(TraceError, match="No endpoint GET /y"):
        resolve_entry(graph, "GET /y", "route")
    with pytest.raises(TraceError, match="called 'nothing'"):
        resolve_entry(graph, "nothing")
    with pytest.raises(TraceError, match="Say where"):
        resolve_entry(graph, "  ")
