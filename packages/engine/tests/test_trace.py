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
    _shares,  # pyright: ignore[reportPrivateUsage]
    feature_entries,
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
    """The way down (a return leg left out)."""
    return [(hop.layer, hop.node.qualified_name) for hop in path.hops[: path.return_index]]


def test_the_login_page_traces_to_the_user_table(template: GraphSnapshot) -> None:
    """Login → its hook → the generated client → the endpoint → the handler → crud → user: the
    code says each hop (checked by hand against the template at b1125d8)."""
    result = trace_feature(template, "frontend/src/routes/login.tsx")
    assert [n.symbol_key for n in result.entries] == ["tsx frontend/src/routes/login.tsx/Login()."]
    best = result.paths[0]
    assert best.reaches_goal
    assert best.breaks == []
    down = best.hops[: best.return_index]
    assert [hop.node.symbol_key for hop in down] == [
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
    layers = [hop.layer for hop in down]
    assert layers[:2] == ["ui", "state"]
    assert layers[3:6] == ["client", "route", "handler"]
    assert layers[-1] == "db"
    # And back up (3.22): useAuth's login() stores the token, the mutation's onSuccess goes
    # home, its onError hands the error to handleError.
    assert [
        (hop.node.name, hop.edge.evidence_line if hop.edge else None)
        for hop in best.hops[len(down) :]
    ] == [
        ("useAuth", 42),
        ("localStorage.setItem access_token", 45),
        ("navigate /", 51),
        ("handleError", 53),
    ]
    # Each hop is proven where the code says it: the handler's call on login.py:30.
    authenticate = best.hops[6]
    edge = authenticate.edge
    assert edge is not None
    assert (edge.type, edge.evidence_path, edge.evidence_line) == (
        EdgeType.CALLS, "backend/app/api/routes/login.py", 30,
    )  # fmt: skip
    assert best.confidence == pytest.approx(0.9**7)
    model = table_model(template, down[-1].node)
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
    assert best.hops[: best.return_index][-1].node.qualified_name == "authenticate"
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
    assert path.breaks[0].kind == "gap"
    lonely = _graph([HANDLER], [])
    [alone] = trace_feature(lonely, HANDLER.symbol_key).paths
    assert alone.breaks[0].reason == "read_x calls no more of the repository's code"
    assert alone.breaks[0].kind == "end"  # on the server: where this way ends, not a gap


def test_client_code_that_calls_nothing_is_a_gap() -> None:
    """Before the request leaves the client, code calling nothing further means the request is
    missing: a gap, and the page's helper on it stays in the page's layer."""
    helper = _node("ts ui.tsx/cn().", NodeKind.FUNCTION, path="ui.tsx", lane_="ui")
    graph = _graph([PAGE, helper], [(PAGE.symbol_key, helper.symbol_key, _edge(EdgeType.CALLS))])
    [path] = trace_feature(graph, PAGE.symbol_key).paths
    assert [h.layer for h in path.hops] == ["ui", "ui"]
    assert path.breaks[0].kind == "gap"
    assert path.breaks[0].reason == "cn calls nothing the trace follows further"


def test_the_ui_code_on_the_way_to_a_request_is_the_clients() -> None:
    """An API client's method between the page and its request is the client's layer, not the
    page's (the Hook / client lane, not three components in a row)."""
    client = _node("ts api.ts/authService.login().", NodeKind.METHOD, path="api.ts", lane_="ui")
    request = _node("http_client_call POST /x api.ts:3", NodeKind.HTTP_CLIENT_CALL, path="api.ts")
    graph = _graph(
        [PAGE, client, request, ENDPOINT, HANDLER, TABLE],
        [
            (PAGE.symbol_key, client.symbol_key, _edge(EdgeType.CALLS)),
            (client.symbol_key, request.symbol_key, _edge(EdgeType.ISSUES_REQUEST)),
            (request.symbol_key, ENDPOINT.symbol_key, _edge(EdgeType.TARGETS)),
            (ENDPOINT.symbol_key, HANDLER.symbol_key, _edge(EdgeType.HANDLED_BY)),
            (HANDLER.symbol_key, TABLE.symbol_key, _edge(EdgeType.READS)),
        ],
    )
    [path] = trace_feature(graph, PAGE.symbol_key).paths
    assert [h.layer for h in path.hops] == ["ui", "state", "client", "route", "handler", "db"]


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


def test_a_method_name_several_classes_have_is_ambiguous() -> None:
    """``repo.save()`` on a receiver of no known type: the resolver's guesses (``name_ambiguous``)
    at one call site are its alternatives."""
    a = _node("py a.py/A#save().", NodeKind.METHOD)
    b = _node("py b.py/B#save().", NodeKind.METHOD)
    guess = Edge(EdgeType.CALLS, 0.1, Provenance.NAME_AMBIGUOUS, "a.py", 5)
    graph = _graph(
        [HANDLER, a, b],
        [(HANDLER.symbol_key, a.symbol_key, guess), (HANDLER.symbol_key, b.symbol_key, guess)],
    )
    paths = trace_feature(graph, HANDLER.symbol_key).paths
    assert {p.hops[1].node.symbol_key for p in paths} == {a.symbol_key, b.symbol_key}
    assert all(p.hops[1].alternatives == 2 for p in paths)


def test_calls_on_their_own_lines_are_not_ambiguous() -> None:
    """A hook making two calls makes two, each certain of its target: neither is ambiguous."""
    hook = _node("ts h.ts/useItems().", NodeKind.HOOK, path="h.ts")
    other = _node("http_client_call GET /y ui.tsx:4", NodeKind.HTTP_CLIENT_CALL, path="ui.tsx")
    graph = _graph(
        [PAGE, hook, CALL, other],
        [
            (PAGE.symbol_key, hook.symbol_key, _edge(EdgeType.USES_HOOK)),
            (hook.symbol_key, CALL.symbol_key, _edge(EdgeType.ISSUES_REQUEST, 1.0, line=3)),
            (hook.symbol_key, other.symbol_key, _edge(EdgeType.ISSUES_REQUEST, 1.0, line=4)),
        ],
    )
    paths = trace_feature(graph, PAGE.symbol_key).paths
    assert {p.hops[2].node.symbol_key for p in paths} == {CALL.symbol_key, other.symbol_key}
    assert all(h.alternatives == 1 for p in paths for h in p.hops)
    # Nor are a handler's dependencies, declared on one line.
    session = _node("py a.py/get_db().", NodeKind.FUNCTION)
    user = _node("py a.py/current_user().", NodeKind.FUNCTION)
    handler = _graph(
        [HANDLER, session, user],
        [
            (HANDLER.symbol_key, session.symbol_key, _edge(EdgeType.INJECTS, line=7)),
            (HANDLER.symbol_key, user.symbol_key, _edge(EdgeType.INJECTS, line=7)),
        ],
    )
    hops = [h for p in trace_feature(handler, HANDLER.symbol_key).paths for h in p.hops]
    assert all(h.alternatives == 1 for h in hops)


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
    assert paths[0].breaks[0].reason == f"the trace shows {MAX_DEPTH} hops; f{MAX_DEPTH} goes on"
    assert paths[0].breaks[0].kind == "limit"
    # A way whose last hop calls nothing more has ended there, however long it is.
    exact = chain[: MAX_DEPTH + 1]
    edges = [(a.symbol_key, b.symbol_key, _edge(EdgeType.CALLS)) for a, b in pairwise(exact)]
    [ended] = trace_feature(_graph(exact, edges), exact[0].symbol_key).paths
    assert len(ended.hops) == MAX_DEPTH + 1
    assert ended.breaks[0].kind != "limit"


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


# ---------------------------------------------------------------- where an entry is reached from


def _keys(path: TracePath) -> list[str]:
    return [h.node.symbol_key for h in path.hops]


def _journey() -> tuple[GraphSnapshot, Node, Node]:
    """A page using a hook whose callback calls the API, the endpoint's handler reading a table;
    and a test driving the handler."""
    hook = _node("ts h.ts/useAuth().", NodeKind.HOOK, path="h.ts")
    inner = _node("ts h.ts/useAuth().login().", NodeKind.FUNCTION, path="h.ts")
    spec = _node("ts e2e/login.spec.ts/", NodeKind.MODULE, path="e2e/login.spec.ts")
    graph = _graph(
        [PAGE, hook, inner, CALL, ENDPOINT, HANDLER, TABLE, spec],
        [
            (PAGE.symbol_key, hook.symbol_key, _edge(EdgeType.USES_HOOK)),
            (inner.symbol_key, CALL.symbol_key, _edge(EdgeType.ISSUES_REQUEST, 1.0, line=9)),
            (CALL.symbol_key, ENDPOINT.symbol_key, _edge(EdgeType.TARGETS, 0.9)),
            (ENDPOINT.symbol_key, HANDLER.symbol_key, _edge(EdgeType.HANDLED_BY, 1.0)),
            (HANDLER.symbol_key, TABLE.symbol_key, _edge(EdgeType.READS)),
            (spec.symbol_key, HANDLER.symbol_key, _edge(EdgeType.CALLS)),
        ],
    )
    return graph, hook, inner


def test_a_function_mid_flow_shows_where_it_is_reached_from_and_where_it_goes() -> None:
    """From the handler: back to the page (through the endpoint, the call, the hook its
    callback is in), on to the table; the test calling the handler is no origin."""
    graph, hook, _ = _journey()
    [path] = trace_feature(graph, HANDLER.symbol_key, direction="both").paths
    assert _keys(path) == [
        PAGE.symbol_key, hook.symbol_key, CALL.symbol_key, ENDPOINT.symbol_key,
        HANDLER.symbol_key, TABLE.symbol_key,
    ]  # fmt: skip
    assert (path.entry_index, path.reaches_origin, path.reaches_goal) == (4, True, True)
    # Each hop says how it is reached from the one before: the hook's callback issues the call.
    assert [h.edge_type for h in path.hops[1:]] == [
        EdgeType.USES_HOOK, EdgeType.ISSUES_REQUEST, EdgeType.TARGETS, EdgeType.HANDLED_BY,
        EdgeType.READS,
    ]  # fmt: skip
    edge = path.hops[2].edge
    assert edge is not None
    assert edge.evidence_line == 9
    assert path.confidence == pytest.approx(0.9 * 1.0 * 0.9 * 1.0 * 0.9)
    # Down alone is the trace as it was; up alone ends at the entry.
    [down] = trace_feature(graph, HANDLER.symbol_key).paths
    assert (_keys(down), down.entry_index) == ([HANDLER.symbol_key, TABLE.symbol_key], 0)
    [up] = trace_feature(graph, HANDLER.symbol_key, direction="up").paths
    assert _keys(up) == _keys(path)[:5]
    assert (up.entry_index, up.reaches_origin) == (4, True)


def test_a_nested_function_is_reached_through_the_one_it_is_in() -> None:
    graph, hook, inner = _journey()
    path = trace_feature(graph, inner.symbol_key, direction="both").paths[0]
    assert _keys(path)[:3] == [PAGE.symbol_key, hook.symbol_key, inner.symbol_key]
    assert path.entry_index == 2
    assert path.hops[2].edge_type is EdgeType.CONTAINS
    assert path.reaches_goal


def test_a_page_is_where_its_journey_starts_and_breaks_count_from_the_page() -> None:
    graph, _, _ = _journey()
    [path] = trace_feature(graph, PAGE.symbol_key, direction="both").paths
    assert (path.entry_index, path.reaches_origin) == (0, True)
    lonely = _graph(
        [PAGE, CALL], [(PAGE.symbol_key, CALL.symbol_key, _edge(EdgeType.ISSUES_REQUEST))]
    )
    [stops] = trace_feature(lonely, CALL.symbol_key, direction="both").paths
    assert (_keys(stops), stops.entry_index) == ([PAGE.symbol_key, CALL.symbol_key], 1)
    assert [(b.after_hop, b.reason) for b in stops.breaks] == [
        (1, "client call has no matching endpoint")
    ]


def test_code_nothing_in_the_app_reaches_has_no_origin() -> None:
    """Called by a test only: the path back stops at the entry, and says it found no page."""
    orphan = _node("py a.py/cleanup().", NodeKind.FUNCTION, lane_="svc")
    spec = _node("py tests/test_a.py/test_cleanup().", NodeKind.FUNCTION, path="tests/test_a.py")
    graph = _graph([orphan, spec], [(spec.symbol_key, orphan.symbol_key, _edge(EdgeType.CALLS))])
    [path] = trace_feature(graph, orphan.symbol_key, direction="up").paths
    assert (_keys(path), path.reaches_origin) == ([orphan.symbol_key], False)


def test_the_app_s_entry_module_is_an_origin() -> None:
    main = _node("tsx src/main.tsx/", NodeKind.MODULE, path="src/main.tsx", lane_="ui")
    graph = _graph([main, PAGE], [(main.symbol_key, PAGE.symbol_key, _edge(EdgeType.RENDERS))])
    [path] = trace_feature(graph, PAGE.symbol_key, direction="up").paths
    assert (_keys(path), path.reaches_origin) == ([main.symbol_key, PAGE.symbol_key], True)


def _endpoint(method: str, route: str) -> Node:
    name = f"{method} {route}"
    return Node(
        f"api_endpoint {name}", NodeKind.API_ENDPOINT.value, name, name, None, 0, 0, "", "api"
    )


def _table(name: str) -> Node:
    return Node(f"table {name}", NodeKind.TABLE.value, name, name, None, 0, 0, "", "db")


def _handler(name: str) -> Node:
    return _node(f"py api.py/{name}().", NodeKind.FUNCTION, path="api.py", lane_="api")


def _orders_graph() -> tuple[GraphSnapshot, dict[str, Node]]:
    """A dashboard that lists items and places orders; the order's handler looks its caller up
    in ``users`` and saves the row in ``orders``."""
    page = _node("ts dash.tsx/Dashboard().", NodeKind.COMPONENT, path="dash.tsx", lane_="ui")
    list_call = _node("http_client_call GET /items dash.tsx:3", NodeKind.HTTP_CLIENT_CALL)
    order_call = _node("http_client_call POST /orders dash.tsx:9", NodeKind.HTTP_CLIENT_CALL)
    items = _endpoint("GET", "/api/items")
    create = _endpoint("POST", "/api/orders")
    mine = _endpoint("GET", "/api/orders/my-orders")
    by_status = _endpoint("GET", "/api/orders/status/{status}")
    list_items, create_order = _handler("list_items"), _handler("create_order")
    my_orders, orders_by_status = _handler("my_orders"), _handler("orders_by_status")
    find_user = _node("py db.py/find_user().", NodeKind.FUNCTION, path="db.py", lane_="db")
    users, orders, item = _table("users"), _table("orders"), _table("item")
    nodes = {
        n.symbol_key: n
        for n in (page, list_call, order_call, items, create, mine, by_status, list_items,
                  create_order, my_orders, orders_by_status, find_user, users, orders, item)
    }  # fmt: skip
    sure = 0.9
    edges = [
        (page, list_call, _edge(EdgeType.ISSUES_REQUEST, sure)),
        (page, order_call, _edge(EdgeType.ISSUES_REQUEST, sure)),
        (list_call, items, _edge(EdgeType.TARGETS, sure)),
        (order_call, create, _edge(EdgeType.TARGETS, sure)),
        (items, list_items, _edge(EdgeType.HANDLED_BY, sure)),
        (create, create_order, _edge(EdgeType.HANDLED_BY, sure)),
        (mine, my_orders, _edge(EdgeType.HANDLED_BY, sure)),
        (by_status, orders_by_status, _edge(EdgeType.HANDLED_BY, sure)),
        (list_items, item, _edge(EdgeType.READS, sure)),
        (create_order, find_user, _edge(EdgeType.CALLS, sure)),
        (find_user, users, _edge(EdgeType.READS, sure)),
        (create_order, orders, _edge(EdgeType.READS, 0.7)),
        (create_order, orders, _edge(EdgeType.WRITES, 0.7)),
        (my_orders, orders, _edge(EdgeType.READS, sure)),
        (orders_by_status, orders, _edge(EdgeType.READS, sure)),
    ]
    keyed = [(a.symbol_key, b.symbol_key, e) for a, b, e in edges]
    return _graph(list(nodes.values()), keyed), {n.name: n for n in nodes.values()}


def test_a_feature_in_words_starts_from_the_endpoint_that_serves_it() -> None:
    """``create order`` is the POST its verb asks for; with no verb, a feature is shown (GET),
    the route naming least besides first; a verb no endpoint of its things does is none."""
    graph, _ = _orders_graph()
    assert [n.name for n in feature_entries(graph, "create order")] == ["POST /api/orders"]
    assert [n.name for n in feature_entries(graph, "place order")] == ["POST /api/orders"]
    assert [n.name for n in feature_entries(graph, "my orders")] == ["GET /api/orders/my-orders"]
    assert feature_entries(graph, "delete order") == []
    with pytest.raises(TraceError, match="no endpoint or code is named for it"):
        trace_feature(graph, "delete order")
    # A name still wins over words: the entry is what is called that.
    assert [n.name for n in resolve_entry(graph, "Dashboard")] == ["Dashboard"]


def test_a_feature_found_by_its_words_is_followed_to_its_own_table() -> None:
    """Past ``POST /api/orders`` the path goes on to ``orders``, not the ``users`` row the
    handler looks the caller up in (surer, and nearer); and of a table read and written as
    surely, the hop is the write."""
    graph, _ = _orders_graph()
    [best, *_] = trace_feature(graph, "create order", direction="both").paths
    down = best.hops[: best.return_index]
    assert [h.node.name for h in down][-3:] == ["POST /api/orders", "create_order", "orders"]
    assert down[-1].edge_type is EdgeType.WRITES
    assert down[0].node.name == "Dashboard"  # where the request is made from


def test_a_feature_says_which_of_a_pages_ways_to_follow() -> None:
    """A dashboard listing items and placing orders: ``feature`` picks the order's way."""
    graph, named = _orders_graph()
    page = named["Dashboard"].symbol_key
    plain = trace_feature(graph, page).paths[0]
    asked = trace_feature(graph, page, feature="place order").paths[0]
    assert plain.hops[-1].node.name == "item"  # the surest way, no feature asked
    assert [h.node.name for h in asked.hops[: asked.return_index]][-1] == "orders"


def test_a_name_with_one_of_the_words_counts_more_than_a_longer_form() -> None:
    words = {"traces"}
    assert _shares(words, _node("ts t.ts/useGetTracesQuery().", NodeKind.HOOK)) == 2
    assert _shares(words, _node("ts t.ts/useGetTraceQuery().", NodeKind.HOOK)) == 1
    assert _shares(words, _node("ts t.ts/useGetFlows().", NodeKind.HOOK)) == 0
