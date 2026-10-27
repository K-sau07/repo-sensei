"""Frontend calls to backend endpoints: normalizing, the segment trie, confidence and the
reasons a call matches nothing; then the fastapi-template, every call to its endpoint: by URL
from the static routes, and by operationId from the running app's (recorded in
fixtures/fastapi-template-routes.json, as the sandbox's introspection printed them)."""

from __future__ import annotations

import json
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.python.imports import extract
from sensei_engine.graph.writer import LINKS, GraphNode
from sensei_engine.link.fastapi import (
    MARKER,
    Route,
    RouteGraph,
    endpoint_facts,
    routes_from_output,
    scan_routes,
    static_routes,
)
from sensei_engine.link.http_calls import (
    CallGraph,
    HttpCall,
    client_calls,
    env_values,
    scan_all,
)
from sensei_engine.link.urls import (
    LITERAL_CONFIDENCE,
    Endpoint,
    config_bases,
    link_calls,
    normalize,
    with_reasons,
)
from sensei_engine.parse.parser import ParseResult, parse_file
from sensei_engine.symbols.extract import extract_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"
RECORDED_ROUTES = Path(__file__).parent / "fixtures" / "fastapi-template-routes.json"


@pytest.mark.parametrize(
    ("url", "path", "literal"),
    [
        ("/api/items/", "/api/items", True),
        ("https://api.example.com/v1/items?x=1#top", "/v1/items", True),
        ("//cdn.example.com/a", "/a", True),
        ("/users/:id", "/users/{}", False),
        ("/users/:id?", "/users/{}", False),
        ("/u/<int:pk>/", "/u/{}", False),
        ("/posts/[slug]", "/posts/{}", False),
        ("/docs/[...path]", "/docs/{*}", False),
        ("/docs/[[...path]]", "/docs/{*}", False),
        ("/files/{name:path}", "/files/{*}", False),
        ("/items/${id}/edit", "/items/{}/edit", False),
        ("/items/v${n}", "/items/{}", False),  # part literal, part value
        ("{env:VITE_API}/items", "/items", True),
        ("", "/", True),
    ],
)
def test_urls_normalize(url: str, path: str, literal: bool) -> None:
    normalized = normalize("get", url)
    assert (normalized.method, normalized.path, normalized.literal) == ("GET", path, literal)
    assert normalized.base_unknown == url.startswith("{env:")


def test_a_base_goes_in_front() -> None:
    assert normalize("POST", "/things/{}", base="/api/v2/").path == "/api/v2/things/{}"


ENDPOINTS = [
    Endpoint("api_endpoint GET /api/users", "GET", "/api/users"),
    Endpoint("api_endpoint GET /api/users/me", "GET", "/api/users/me"),
    Endpoint("api_endpoint GET /api/users/{}", "GET", "/api/users/{}"),
    Endpoint("api_endpoint DELETE /api/users/{}", "DELETE", "/api/users/{}"),
    Endpoint("api_endpoint GET /api/{}/items", "GET", "/api/{}/items"),
    Endpoint("api_endpoint GET /api/{}/{}", "GET", "/api/{}/{}"),
    Endpoint("api_endpoint GET /static/{*}", "GET", "/static/{*}"),
    Endpoint("api_endpoint POST /login", "POST", "/login", operation_id="login-access"),
]


def _call(key: str, method: str, url: str, **extra: object) -> HttpCall:
    return HttpCall(key=key, method=method, url=url, **extra)  # type: ignore[arg-type]


def _targets(
    calls: list[HttpCall], bases: tuple[str, ...] = ()
) -> dict[str, list[tuple[str, float]]]:
    linked = link_calls(calls, ENDPOINTS, bases=bases)
    found: dict[str, list[tuple[str, float]]] = {}
    for edge in linked.edges:
        assert edge.type is EdgeType.TARGETS
        found.setdefault(edge.src, []).append((edge.dst, round(edge.confidence or 0, 3)))
    return found


def test_literal_segments_outrank_parameters() -> None:
    found = _targets(
        [
            _call("me", "GET", "/api/users/me"),
            _call("by-id", "GET", "/api/users/${id}"),
            _call("bob", "get", "/api/users/bob"),  # a literal on a parameter: matched, unsure
        ]
    )
    assert found["me"] == [("api_endpoint GET /api/users/me", LITERAL_CONFIDENCE)]
    assert found["by-id"] == [("api_endpoint GET /api/users/{}", LITERAL_CONFIDENCE)]
    assert found["bob"] == [("api_endpoint GET /api/users/{}", 0.5)]


def test_ties_are_ambiguous_and_split_the_confidence() -> None:
    found = _targets([_call("x", "GET", "/api/${a}/${b}")])
    assert found["x"] == [("api_endpoint GET /api/{}/{}", LITERAL_CONFIDENCE)]
    found = _targets([_call("y", "GET", "/api/${a}/items")])
    # /api/{}/items has one more literal than /api/{}/{}: it wins outright.
    assert found["y"] == [("api_endpoint GET /api/{}/items", LITERAL_CONFIDENCE)]
    found = _targets([_call("z", "GET", "/api/users/items")])
    assert sorted(found["z"]) == [
        ("api_endpoint GET /api/users/{}", 0.25),
        ("api_endpoint GET /api/{}/items", 0.25),
    ]


def test_catch_alls_contracts_and_bases() -> None:
    found = _targets(
        [
            _call("asset", "GET", "/static/js/app.js"),
            _call("login", "POST", "/whatever", operation_id="login-access"),
            _call("proxied", "GET", "/users/me"),  # under a base the dev proxy adds
            _call("instance", "DELETE", "/users/${id}", base="/api"),
        ],
        bases=("/api",),
    )
    assert found["asset"] == [("api_endpoint GET /static/{*}", 0.5)]
    assert found["login"] == [("api_endpoint POST /login", 1.0)]
    assert found["proxied"] == [("api_endpoint GET /api/users/me", LITERAL_CONFIDENCE)]
    assert found["instance"] == [("api_endpoint DELETE /api/users/{}", LITERAL_CONFIDENCE)]
    linked = link_calls([_call("login", "POST", "/x", operation_id="login-access")], ENDPOINTS)
    assert linked.edges[0].provenance is Provenance.CONTRACT
    assert linked.report.contract == 1


def test_unmatched_calls_say_why() -> None:
    linked = link_calls(
        [
            _call("method", "POST", "/api/users/me"),
            _call("dynamic", "GET", "", dynamic=True),
            _call("env", "GET", "{env:VITE_API}/nowhere"),
            _call("none", "GET", "/nothing/here"),
        ],
        ENDPOINTS,
    )
    reasons = linked.report.unmatched
    # GET /api/users/me, and DELETE /api/users/{} whose parameter takes "me".
    assert reasons["method"] == "no POST endpoint at /api/users/me (it has DELETE, GET)"
    assert reasons["dynamic"].startswith("the URL is built at run time")
    assert reasons["env"].startswith("its base URL comes from the environment")
    assert reasons["none"] == "no endpoint matches GET /nothing/here"
    assert linked.edges == []


def test_an_operation_id_links_however_the_generator_spells_it() -> None:
    endpoints = [
        Endpoint("api_endpoint POST /login", "POST", "/login", "login-login_access_token"),
        Endpoint("api_endpoint GET /a", "GET", "/a", "read_items_api_v1_items__get"),
    ]
    linked = link_calls(
        [
            _call("hey", "POST", "/elsewhere", operation_id="loginLoginAccessToken"),
            _call("codegen", "GET", "/b", operation_id="readItemsApiV1ItemsGet"),
            _call("none", "GET", "/a", operation_id="noSuchOperation"),  # then by its URL
        ],
        endpoints,
    )
    found = {e.src: (e.dst, e.provenance, e.confidence) for e in linked.edges}
    assert found["hey"] == ("api_endpoint POST /login", Provenance.CONTRACT, 1.0)
    assert found["codegen"] == ("api_endpoint GET /a", Provenance.CONTRACT, 1.0)
    assert found["none"] == ("api_endpoint GET /a", Provenance.URL_LITERAL, LITERAL_CONFIDENCE)


def test_an_operation_id_must_agree_with_the_calls_method_and_url() -> None:
    endpoints = [
        Endpoint("api_endpoint POST /login", "POST", "/login", "login"),
        Endpoint("api_endpoint GET /users/me", "GET", "/users/me", "read_me"),
        Endpoint("api_endpoint POST /items", "POST", "/items", "create_item"),
    ]
    linked = link_calls(
        [
            _call("agrees", "POST", "/login", operation_id="login"),
            _call("no-url", "POST", "/gone", operation_id="login"),
            _call("other-method", "GET", "/users/me", operation_id="login"),
            _call("stale", "POST", "/items", operation_id="login"),  # its URL says otherwise
        ],
        endpoints,
    )
    found = {e.src: (e.dst, e.provenance, e.confidence) for e in linked.edges}
    assert found["agrees"] == ("api_endpoint POST /login", Provenance.CONTRACT, 1.0)
    assert found["no-url"] == ("api_endpoint POST /login", Provenance.CONTRACT, 1.0)
    assert found["other-method"] == (
        "api_endpoint GET /users/me",
        Provenance.URL_LITERAL,
        LITERAL_CONFIDENCE,
    )
    assert found["stale"] == (
        "api_endpoint POST /items",
        Provenance.URL_LITERAL,
        LITERAL_CONFIDENCE,
    )
    assert linked.report.contract == 2


def test_a_base_dropped_for_the_match_is_unsure() -> None:
    endpoints = [Endpoint("api_endpoint GET /items/{}", "GET", "/items/{}")]
    found = {
        e.src: (e.dst, e.confidence)
        for e in link_calls(
            [
                _call("own-base", "GET", "/${id}", base="/items"),
                _call("dropped", "GET", "/items/${id}", base="/v2"),  # only without its base
            ],
            endpoints,
        ).edges
    }
    assert found["own-base"] == ("api_endpoint GET /items/{}", LITERAL_CONFIDENCE)
    assert found["dropped"] == ("api_endpoint GET /items/{}", 0.5)


def test_an_endpoint_is_matched_under_its_apps_root_path() -> None:
    endpoints = [Endpoint("api_endpoint GET /items/{}", "GET", "/items/{}", root_path="/backend")]
    found = {
        e.src: e.dst
        for e in link_calls(
            [_call("proxied", "GET", "/backend/items/${id}"), _call("direct", "GET", "/items/1")],
            endpoints,
        ).edges
    }
    assert found == {
        "proxied": "api_endpoint GET /items/{}",
        "direct": "api_endpoint GET /items/{}",
    }


def test_an_unmatched_call_says_why_on_its_node() -> None:
    linked = link_calls(
        [_call("c1", "GET", "/nothing"), _call("c2", "GET", "/api/users")], ENDPOINTS
    )
    nodes = with_reasons(
        [
            GraphNode("c1", NodeKind.HTTP_CLIENT_CALL, "GET /nothing"),
            GraphNode("c2", NodeKind.HTTP_CLIENT_CALL, "GET /api/users"),
        ],
        linked.report,
    )
    assert nodes[0].attrs == {"unmatched_reason": "no endpoint matches GET /nothing"}
    assert nodes[1].attrs == {}


def test_bases_from_vite_and_next_config() -> None:
    configs = {
        "web/vite.config.ts": (
            "export default defineConfig({ server: { proxy: {\n"
            "  '/api': 'http://localhost:8000',\n"
            "  \"/auth/\": { target: 'http://localhost:9000' },\n"
            "} } })"
        ),
        "site/next.config.js": (
            "module.exports = { async rewrites() { return [\n"
            "  { source: '/backend/:path*', destination: 'http://api/:path*' },\n"
            "] } }"
        ),
    }
    assert config_bases(configs) == ["/api", "/auth", "/backend"]


def _template(tmp_path: Path) -> tuple[Project, list[Route], CallGraph, list[str]]:
    """The template's project, static routes, frontend calls and config bases."""
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    base = tmp_path / "fastapi-template"
    texts = {
        str(p.relative_to(base)): p.read_text(errors="replace")
        for p in base.rglob("*")
        if p.is_file()
        and "node_modules" not in p.parts
        and (p.suffix in (".py", ".ts", ".tsx") or is_config(p.name) or p.name.startswith(".env"))
    }
    definitions: dict[str, list[Definition]] = {}
    parsed: dict[str, ParseResult] = {}
    for path, text in texts.items():
        if path.endswith((".py", ".ts", ".tsx")):
            result = parse_file(path, text.encode())
            if result.language is None:
                continue
            parsed[path] = result
            definitions[path] = [
                Definition(
                    s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
                )
                for s in extract_symbols(result, result.language)
            ]
    configs = {p: t for p, t in texts.items() if is_config(p)}
    project = Project(frozenset(texts), configs, definitions)
    python = {p: r for p, r in parsed.items() if p.endswith(".py")}
    routes = static_routes(
        project,
        {p: scan_routes(r, definitions[p]) for p, r in python.items()},
        {p: extract(r) for p, r in python.items()},
    )
    frontend = [(r, definitions[p]) for p, r in parsed.items() if p.startswith("frontend/src/")]
    envs = env_values(project, {p: t for p, t in texts.items() if "/.env" in f"/{p}"})
    calls = client_calls(project, scan_all(frontend, envs))
    return project, routes, calls, config_bases(configs)


def _endpoints(graph: RouteGraph) -> list[Endpoint]:
    return [
        Endpoint(
            n.key,
            n.attrs["method"],
            n.attrs["path_norm"],
            n.attrs.get("operation_id"),
            n.attrs.get("root_path", ""),
        )
        for n in graph.nodes
    ]


def test_the_template_every_call_reaches_its_endpoint(tmp_path: Path) -> None:
    project, routes, calls, bases = _template(tmp_path)
    linked = link_calls(calls.calls, _endpoints(endpoint_facts(project, routes, None)), bases=bases)
    LINKS.check(calls.nodes, [*calls.edges, *linked.edges])
    assert linked.report.calls == 23
    assert linked.report.unmatched == {}
    assert linked.report.ambiguous == 0
    # Every call to the endpoint of the same method and path, sure of it.
    for edge in linked.edges:
        _, method, template, _ = edge.src.split(" ")
        assert edge.dst == f"api_endpoint {method} {normalize(method, template).path}"
        assert edge.confidence == LITERAL_CONFIDENCE


def test_the_template_by_operation_id_from_the_running_app(tmp_path: Path) -> None:
    """The running app's routes (recorded) carry operationIds and the generated SDK's calls name
    theirs: every call is a contract link. Matched by URL alone, the same calls find the same
    endpoints: the trie's precision on the template, against the contract."""
    project, routes, calls, bases = _template(tmp_path)
    recorded = json.loads(RECORDED_ROUTES.read_text())
    runtime = routes_from_output(f"{MARKER}{json.dumps(recorded)}\n", project)
    assert runtime.limitation is None
    graph = endpoint_facts(project, routes, runtime)
    # 3.8: the static routes are the running app's, one for one.
    assert (graph.report.runtime, graph.report.static, graph.report.static_only) == (23, 23, 0)
    assert graph.report.endpoints == 23
    assert all(n.attrs.get("operation_id") for n in graph.nodes)

    endpoints = _endpoints(graph)
    contract = link_calls(calls.calls, endpoints, bases=bases)
    assert (contract.report.linked, contract.report.contract) == (23, 23)
    assert {(e.provenance, e.confidence) for e in contract.edges} == {(Provenance.CONTRACT, 1.0)}
    by_url = link_calls(
        [replace(c, operation_id=None) for c in calls.calls], endpoints, bases=bases
    )
    assert by_url.report.contract == 0
    assert {(e.src, e.dst) for e in by_url.edges} == {(e.src, e.dst) for e in contract.edges}
