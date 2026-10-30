"""Spring routes to endpoints (3.19): a made-up Spring Boot shop of our own, its controllers'
class prefixes, every mapping annotation, arrays, constants (the class's, another's, a static
import's, concatenated), path variables with regexes, mappings an interface declares, the
servlet's context path from properties and YAML (a profile's only as a root path), a
``@Controller`` that renders views, and a client call through ``resolve_links`` to its handler,
with the server's ``/api`` prefix the client's base URL doesn't have."""

from __future__ import annotations

import pickle
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.link import http_calls
from sensei_engine.link.spring import (
    ANY_METHODS,
    Array,
    Concat,
    Lit,
    Name,
    arguments,
    config_root,
    normalize_path,
    parse_value,
    servlet_prefixes,
)
from sensei_engine.link.stage import (
    FileLinks,
    LinkBuild,
    LinkRequest,
    env_files,
    extract_request,
    links_language,
    resolve_links,
)
from sensei_engine.link.urls import PREFIX_CONFIDENCE, Endpoint, link_calls
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

JAVA = "shop/src/main/java/org/acme/shop"
LANGUAGES = {".java": "java", ".js": "javascript", ".ts": "typescript"}

SHOP = {
    f"{JAVA}/web/Paths.java": """\
package org.acme.shop.web;

public final class Paths {
    public static final String API = "/api";
    public static final String V1 = API + "/v1";
    public static final String ITEMS = V1 + "/items";
    public static final String[] BOTH = {"/one", "/two"};
    public static String notFinal = "/nope";
}
""",
    f"{JAVA}/web/ItemController.java": """\
package org.acme.shop.web;

import java.util.List;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping(Paths.ITEMS)
public class ItemController {
    private static final String ONE = "/{id:[0-9]{1,6}}";

    @GetMapping
    public List<String> list() { return List.of(); }

    @GetMapping(ONE)
    public String get(@PathVariable("id") long id) { return ""; }

    @PostMapping(value = "")
    public String create(@RequestBody String body) { return body; }

    @PutMapping(path = ONE)
    public String replace(@PathVariable long id) { return ""; }

    @PatchMapping(ONE + "/name")
    public String rename(@PathVariable long id) { return ""; }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> remove(@PathVariable long id) { return null; }

    @RequestMapping(value = "/search", method = RequestMethod.GET)
    public List<String> search() { return List.of(); }

    @RequestMapping(path = "/bulk", method = {RequestMethod.POST, RequestMethod.PUT})
    public void bulk() { }

    @RequestMapping("/ping")
    public String ping() { return "pong"; }

    @GetMapping("/ping")
    public String pingGet() { return "pong"; }

    @GetMapping(Paths.BOTH)
    public String either() { return ""; }

    @GetMapping("/files/{*rest}")
    public String file() { return ""; }

    @GetMapping(Paths.notFinal)
    public String unknown() { return ""; }

    private String helper() { return ""; }
}
""",
    f"{JAVA}/web/AdminController.java": """\
package org.acme.shop.web;

import static org.acme.shop.web.Paths.API;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping({API + "/admin", "/legacy/admin"})
public class AdminController {
    @GetMapping({"/stats", "/numbers"})
    public String stats() { return ""; }
}
""",
    f"{JAVA}/web/OrderApi.java": """\
package org.acme.shop.web;

import org.springframework.web.bind.annotation.*;

@RequestMapping("/api/orders")
public interface OrderApi {
    String MINE = "/mine";

    @GetMapping(MINE)
    String mine();

    @PostMapping
    String place(@RequestBody String order);
}
""",
    f"{JAVA}/web/OrderController.java": """\
package org.acme.shop.web;

import org.springframework.web.bind.annotation.RestController;

@RestController
public class OrderController implements OrderApi {
    @Override
    public String mine() { return ""; }

    @Override
    public String place(String order) { return order; }
}
""",
    f"{JAVA}/web/PageController.java": """\
package org.acme.shop.web;

import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.ResponseBody;

@Controller
public class PageController {
    @GetMapping("/home")
    public String home() { return "home"; }

    @GetMapping("/api/pages/raw")
    @ResponseBody
    public String raw() { return ""; }

    @GetMapping("/api/pages/entity")
    public ResponseEntity<String> entity() { return null; }
}
""",
    f"{JAVA}/web/FragmentController.java": """\
package org.acme.shop.web;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.ResponseBody;

@Controller
@ResponseBody
public class FragmentController {
    @GetMapping("/api/fragments")
    public String fragments() { return ""; }
}
""",
    f"{JAVA}/ours/GetMapping.java": """\
package org.acme.shop.ours;

public @interface GetMapping {
    String value() default "";
}
""",
    f"{JAVA}/web/NotSpringController.java": """\
package org.acme.shop.web;

import org.acme.shop.ours.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class NotSpringController {
    @GetMapping("/ours")
    public String ours() { return ""; }
}
""",
    f"{JAVA}/service/Helper.java": """\
package org.acme.shop.service;

public class Helper {
    public String run() { return ""; }
}
""",
}


def _files(root: Path, files: Mapping[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _language(path: str) -> str | None:
    return LANGUAGES.get(Path(path).suffix)


def _build(root: Path, files: Mapping[str, str]) -> tuple[LinkBuild, dict[str, FileLinks]]:
    """The links stage over ``files`` written under ``root``, as the pipeline runs it."""
    _files(root, files)
    paths = sorted(files)
    definitions: dict[str, tuple[Definition, ...]] = {}
    for path in paths:
        language = _language(path)
        if language is None:
            continue
        parsed = parse_file(path, files[path].encode(), language=language)
        definitions[path] = tuple(
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, language)
        )
    project = Project(
        frozenset(paths), {p: t for p, t in files.items() if is_config(p)}, definitions
    )
    envs = http_calls.env_values(project, {p: files[p] for p in env_files(paths)})
    linked = {
        path: extract_request(
            LinkRequest(
                str(root),
                path,
                language,
                http_calls.env_for(path, envs) if language != "java" else {},
            )
        )
        for path in paths
        if (language := _language(path)) is not None and links_language(language)
    }
    return resolve_links(project, linked), linked


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> tuple[LinkBuild, dict[str, FileLinks]]:
    return _build(tmp_path_factory.mktemp("shop"), SHOP)


def _endpoints(build: LinkBuild) -> dict[str, str]:
    """Endpoint key: its handler's key."""
    handled = {e.src: e.dst for e in build.edges if e.type is EdgeType.HANDLED_BY}
    return {n.key: handled.get(n.key, "") for n in build.nodes if n.kind.value == "api_endpoint"}


def _handler(cls: str, method: str) -> str:
    return f"java {JAVA}/web/{cls}.java/{cls}#{method}()."


def test_mappings_under_the_class_prefix(shop: tuple[LinkBuild, dict[str, FileLinks]]) -> None:
    endpoints = _endpoints(shop[0])
    items = "/api/v1/items"
    expected = {
        f"GET {items}": "list",
        f"GET {items}/{{}}": "get",
        f"POST {items}": "create",
        f"PUT {items}/{{}}": "replace",
        f"PATCH {items}/{{}}/name": "rename",
        f"DELETE {items}/{{}}": "remove",
        f"GET {items}/search": "search",
        f"POST {items}/bulk": "bulk",
        f"PUT {items}/bulk": "bulk",
        f"GET {items}/one": "either",
        f"GET {items}/two": "either",
        f"GET {items}/files/{{*}}": "file",
    }
    for route, method in expected.items():
        assert endpoints[f"api_endpoint {route}"] == _handler("ItemController", method), route


def test_a_request_mapping_without_a_method_takes_every_method(
    shop: tuple[LinkBuild, dict[str, FileLinks]],
) -> None:
    endpoints = _endpoints(shop[0])
    for verb in ANY_METHODS:
        key = f"api_endpoint {verb} /api/v1/items/ping"
        # The mapping that names GET wins its path over the one that takes every method.
        method = "pingGet" if verb == "GET" else "ping"
        assert endpoints[key] == _handler("ItemController", method)
    node = next(n for n in shop[0].nodes if n.key == "api_endpoint POST /api/v1/items/ping")
    assert node.attrs["any_method"] is True
    assert shop[0].details["spring"]["any_method"] == 1
    assert shop[0].details["spring"]["duplicates"] == 1


def test_arrays_and_constants_give_every_combination(
    shop: tuple[LinkBuild, dict[str, FileLinks]],
) -> None:
    endpoints = _endpoints(shop[0])
    for prefix in ("/api/admin", "/legacy/admin"):  # a static import concatenated, a literal
        for path in ("/stats", "/numbers"):
            assert endpoints[f"api_endpoint GET {prefix}{path}"] == _handler(
                "AdminController", "stats"
            )
    report = shop[0].details["spring"]
    assert report["constants"] >= 6
    # A field that isn't final isn't a constant: the mapping is left out, and where it is said.
    assert not any("unknown" in handler for handler in endpoints.values())
    assert report["unresolved"] == [f"{JAVA}/web/ItemController.java:48 Paths.notFinal"]


def test_mappings_an_interface_declares(shop: tuple[LinkBuild, dict[str, FileLinks]]) -> None:
    endpoints = _endpoints(shop[0])
    assert endpoints["api_endpoint GET /api/orders/mine"] == _handler("OrderController", "mine")
    assert endpoints["api_endpoint POST /api/orders"] == _handler("OrderController", "place")
    assert shop[0].details["spring"]["from_supertypes"] == 2


def test_a_controller_that_renders_views(shop: tuple[LinkBuild, dict[str, FileLinks]]) -> None:
    endpoints = _endpoints(shop[0])
    assert "api_endpoint GET /home" not in endpoints  # a view's name: a page, not an API
    assert endpoints["api_endpoint GET /api/pages/raw"] == _handler("PageController", "raw")
    assert endpoints["api_endpoint GET /api/pages/entity"] == _handler("PageController", "entity")
    assert endpoints["api_endpoint GET /api/fragments"] == _handler(
        "FragmentController", "fragments"
    )
    assert shop[0].details["spring"]["views"] == 1


def test_an_annotation_of_the_repository_is_not_springs(
    shop: tuple[LinkBuild, dict[str, FileLinks]],
) -> None:
    assert "api_endpoint GET /ours" not in _endpoints(shop[0])
    assert shop[0].details["spring"]["controllers"] == 6


def test_endpoint_nodes_and_their_edges(shop: tuple[LinkBuild, dict[str, FileLinks]]) -> None:
    build = shop[0]
    node = next(n for n in build.nodes if n.key == "api_endpoint GET /api/v1/items/{}")
    assert node.name == "GET /api/v1/items/{id:[0-9]{1,6}}"
    assert node.path == f"{JAVA}/web/ItemController.java"
    assert (node.start_line, node.end_line) == (15, 16)  # from its annotation
    assert node.lang == "java"
    assert node.attrs == {
        "method": "GET",
        "path_raw": "/api/v1/items/{id:[0-9]{1,6}}",
        "path_norm": "/api/v1/items/{}",
        "framework": "spring",
        "source": "static",
        "handler": _handler("ItemController", "get"),
        "controller": "org.acme.shop.web.ItemController",
        "module": "",
    }
    edge = next(e for e in build.edges if e.src == node.key)
    assert edge.type is EdgeType.HANDLED_BY
    assert edge.provenance is Provenance.CONTRACT
    assert edge.confidence == 1.0
    assert (edge.evidence_path, edge.evidence_line) == (f"{JAVA}/web/ItemController.java", 15)
    assert edge.attrs == {"source": "static", "annotation": "GetMapping"}


def test_the_facts_leave_the_pool(shop: tuple[LinkBuild, dict[str, FileLinks]]) -> None:
    held = shop[1][f"{JAVA}/web/Paths.java"]
    assert held.spring is not None
    assert held.spring.facts.calls == ()
    assert held.spring.constants[f"java {JAVA}/web/Paths.java/Paths#V1."] == 'API + "/v1"'
    assert pickle.loads(pickle.dumps(held)) == held  # noqa: S301 - crosses the pool's boundary


# ---------------------------------------------------------------- the pieces


def test_annotation_arguments() -> None:
    assert arguments('("/x")') == {"value": '"/x"'}
    assert arguments('(value = {"/a", "/b"}, method = RequestMethod.GET)') == {
        "value": '{"/a", "/b"}',
        "method": "RequestMethod.GET",
    }
    assert arguments(None) == {}
    assert arguments('(path = "/a,b", produces = "x")')["path"] == '"/a,b"'
    assert parse_value('A.B + "/x" + C') == Concat((Name("A.B"), Lit("/x"), Name("C")))
    assert parse_value('{"/a", ("/b")}') == Array((Lit("/a"), Lit("/b")))
    assert parse_value('"a\\"b"') == Lit('a"b')
    assert parse_value("prefix()") is None
    assert parse_value("flag ? A : B") is None


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("/users/{id}", "/users/{}"),
        ("/users/{id:[0-9]{2,3}}/x", "/users/{}/x"),
        ("users//{id}/", "/users/{}"),
        ("/static/**", "/static/{*}"),
        ("/files/{*path}", "/files/{*}"),
        ("/a/*/b", "/a/{}/b"),
        ("", "/"),
    ],
)
def test_paths(raw: str, norm: str) -> None:
    assert normalize_path(raw) == norm


def test_the_servlet_prefix_from_properties_and_yaml() -> None:
    prefixes = servlet_prefixes(
        {
            "a/src/main/resources/application.properties": (
                "# the app\nserver.port=8080\nserver.servlet.context-path=/shop\n"
                "spring.mvc.servlet.path=/mvc\n"
            ),
            "b/src/main/resources/application.yml": "server:\n  servlet:\n    contextPath: /b/\n",
            "c/src/main/resources/application.yml": (
                "server:\n  port: 80\n---\nspring:\n  config:\n    activate:\n"
                "      on-profile: cloud\nserver:\n  servlet:\n    context-path: /cloud\n"
            ),
            "d/src/main/resources/application-prod.properties": "server.servlet.context-path=/p\n",
            "e/src/main/resources/config/application.yaml": (
                "server:\n  servlet:\n    context-path: ${CONTEXT:/env}\n"
            ),
            "f/application.properties": "server.servlet.context-path=${CONTEXT}\n",
            "g/src/test/resources/application.properties": "server.servlet.context-path=/t\n",
            "h/src/main/resources/application.properties": "server.port=1\n",
        }
    )
    assert prefixes["a"].path == "/shop/mvc"
    assert prefixes["b"].path == "/b"
    assert prefixes["c"].path == ""
    assert prefixes["c"].profiles == (("cloud", "/cloud"),)
    assert prefixes["d"].profiles == (("prod", "/p"),)
    assert prefixes["e"].path == "/env"
    assert prefixes["f"].unresolved == ("${CONTEXT}",)
    assert "g" not in prefixes  # a test's resources
    assert "h" not in prefixes
    assert config_root("g/src/test/resources/application.yml") is None
    assert config_root("svc/application-dev.yml") == "svc"
    assert is_config("svc/src/main/resources/application-dev.yaml")


# ---------------------------------------------------------------- end to end

CLIENT = {
    "web/.env": "VITE_API_URL=http://localhost:8080\n",
    "web/src/api.js": """\
import axios from "axios";

const BASE = import.meta.env.VITE_API_URL;

export async function loadItems() {
  return axios.get(`${BASE}/items`);
}

export async function loadItem() {
  const id = window.location.hash;
  return axios.get(`${BASE}/items/${id}`);
}

export async function geocode() {
  const q = window.location.search;
  return fetch(`https://maps.example.com/items/${q}`);
}
""",
    "server/src/main/resources/application.properties": "server.port=8080\n",
    "server/src/main/java/org/acme/catalog/ItemController.java": """\
package org.acme.catalog;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/items")
public class ItemController {
    @GetMapping
    public String list() { return ""; }

    @GetMapping("/{id}")
    public String get(@PathVariable String id) { return ""; }
}
""",
    "server/src/main/java/org/acme/catalog/HealthController.java": """\
package org.acme.catalog;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/health")
public class HealthController {
    @GetMapping
    public String health() { return ""; }
}
""",
}


def _targets(build: LinkBuild) -> dict[str, tuple[str, float, Mapping[str, Any]]]:
    """A call's line: the endpoint it targets, how surely, and the edge's attrs."""
    calls = {n.key: n for n in build.nodes if n.kind.value == "http_client_call"}
    return {
        f"{calls[e.src].path}:{calls[e.src].start_line}": (e.dst, e.confidence or 0.0, e.attrs)
        for e in build.edges
        if e.type is EdgeType.TARGETS
    }


def test_a_call_missing_the_servers_prefix_matches_with_less_confidence(tmp_path: Path) -> None:
    build, _ = _build(tmp_path, CLIENT)
    targets = _targets(build)
    endpoint, confidence, attrs = targets["web/src/api.js:6"]
    assert endpoint == "api_endpoint GET /api/items"
    assert confidence == PREFIX_CONFIDENCE
    assert attrs["prefix_added"] == "/api"
    assert "every route of its server" in str(attrs["reason"])
    assert targets["web/src/api.js:11"][0] == "api_endpoint GET /api/items/{}"
    # A call to another host is not the repository's server: no prefix guessed for it.
    assert "web/src/api.js:16" not in targets
    assert build.details["urls"]["prefixed"] == 2
    handled = {e.src: e.dst for e in build.edges if e.type is EdgeType.HANDLED_BY}
    assert (
        handled[endpoint]
        == "java server/src/main/java/org/acme/catalog/ItemController.java/ItemController#list()."
    )


def test_the_context_path_makes_the_match_exact(tmp_path: Path) -> None:
    files = {
        **CLIENT,
        "web/.env": "VITE_API_URL=http://localhost:8080/shop/api\n",
        "server/src/main/resources/application.properties": "server.servlet.context-path=/shop\n",
    }
    build, _ = _build(tmp_path, files)
    targets = _targets(build)
    for line, endpoint in ((6, "/shop/api/items"), (11, "/shop/api/items/{}")):
        found, confidence, attrs = targets[f"web/src/api.js:{line}"]
        assert (found, confidence) == (f"api_endpoint GET {endpoint}", 0.9)
        assert "prefix_added" not in attrs
    node = next(n for n in build.nodes if n.key == "api_endpoint GET /shop/api/items")
    assert node.attrs["context_path"] == "/shop"
    assert node.attrs["path_raw"] == "/shop/api/items"
    assert build.details["spring"]["context_paths"] == {"server": "/shop"}


def test_a_call_with_the_context_path_but_not_the_rest(tmp_path: Path) -> None:
    files = {
        **CLIENT,
        "web/.env": "VITE_API_URL=http://localhost:8080/shop\n",
        "server/src/main/resources/application.properties": "server.servlet.context-path=/shop\n",
    }
    found, confidence, attrs = _targets(_build(tmp_path, files)[0])["web/src/api.js:11"]
    assert (found, confidence) == ("api_endpoint GET /shop/api/items/{}", PREFIX_CONFIDENCE)
    assert attrs["prefix_added"] == "/api"


def test_a_profiles_context_path_is_a_root_path(tmp_path: Path) -> None:
    files = {
        **CLIENT,
        "web/.env": "VITE_API_URL=http://localhost:8080/cloud/api\n",
        "server/src/main/resources/application-cloud.yml": (
            "server:\n  servlet:\n    context-path: /cloud\n"
        ),
    }
    build, _ = _build(tmp_path, files)
    node = next(n for n in build.nodes if n.key == "api_endpoint GET /api/items")
    assert node.attrs["root_path"] == "/cloud"
    endpoint, confidence, attrs = _targets(build)["web/src/api.js:6"]
    assert endpoint == "api_endpoint GET /api/items"
    assert confidence == 0.9  # matched under the root path, exactly
    assert "prefix_added" not in attrs


def test_a_call_with_a_prefix_the_server_lacks() -> None:
    endpoints = [
        Endpoint("api_endpoint GET /items", "GET", "/items"),
        Endpoint("api_endpoint GET /users", "GET", "/users"),
    ]
    call = http_calls.HttpCall("call 1", "GET", "/backend/items")
    linked = link_calls([call], endpoints)
    (edge,) = linked.edges
    assert edge.dst == "api_endpoint GET /items"
    assert edge.confidence == PREFIX_CONFIDENCE
    assert edge.attrs["prefix_dropped"] == "/backend"
    far = http_calls.HttpCall("call 2", "GET", "https://elsewhere.example.org/x/items")
    assert link_calls([far], endpoints).edges == []


# ---------------------------------------------------------------- hardening (3.19 b)


def test_yaml_aliases_never_multiply_or_loop_the_settings() -> None:
    """A mapping that holds itself is skipped where it recurs; aliases of aliases stop at a
    budget rather than multiply; the settings around them are still read."""
    looped = "a: &a {b: *a}\nserver:\n  servlet:\n    context-path: /shop\n"
    assert servlet_prefixes({"x/application.yml": looped})["x"].path == "/shop"
    bomb = "a0: &a0 {p: 1, q: 2, r: 3, s: 4, t: 5, u: 6}\n" + "".join(
        f"a{i}: &a{i} {{p: *a{i - 1}, q: *a{i - 1}, r: *a{i - 1}, s: *a{i - 1}}}\n"
        for i in range(1, 12)
    )
    tail = "server:\n  servlet:\n    context-path: /y\n"
    started = time.monotonic()
    found = servlet_prefixes({"y/application.yml": bomb + tail})
    assert time.monotonic() - started < 2
    # Past the budget the rest of the document goes unread: no prefix, never a wrong one.
    assert found.get("y") is None or found["y"].path in ("", "/y")


def test_constants_of_constants_are_worked_out_once_and_never_grow_a_route_past_a_cap(
    tmp_path: Path,
) -> None:
    """``C3 = C2 + C2 + ...`` (valid Java) once built megabyte keys, re-evaluated each name:
    each constant is worked out once, and a route past the cap is no route."""
    wide = 64
    levels = "\n".join(
        f"    static final String C{i} = " + " + ".join([f"C{i - 1}"] * wide) + ";"
        for i in range(1, 4)
    )
    files = {
        f"{JAVA}/web/Paths.java": (
            "package org.acme.shop.web;\n\npublic final class Paths {\n"
            f'    static final String C0 = "/a";\n{levels}\n'
            '    static final String SHORT = C0 + "/b";\n}\n'
        ),
        f"{JAVA}/web/Long.java": (
            "package org.acme.shop.web;\n\nimport org.springframework.web.bind.annotation.*;\n\n"
            "@RestController\npublic class Long {\n"
            '    @GetMapping(Paths.C3)\n    public String huge() { return ""; }\n\n'
            '    @GetMapping(Paths.SHORT)\n    public String fine() { return ""; }\n}\n'
        ),
    }
    started = time.monotonic()
    build, _ = _build(tmp_path, files)
    assert time.monotonic() - started < 5
    endpoints = _endpoints(build)
    assert "api_endpoint GET /a/b" in endpoints
    assert all(len(key) < 4096 for key in endpoints)


def _routes_of(tmp_path: Path, constants: Sequence[str], mapped: Sequence[str]) -> list[str]:
    """The routes of a controller mapping each of ``mapped`` (in order) over ``constants``."""
    methods = "".join(
        f'    @GetMapping(Paths.{name})\n    public String m{i}() {{ return ""; }}\n\n'
        for i, name in enumerate(mapped)
    )
    files = {
        f"{JAVA}/web/Paths.java": (
            "package org.acme.shop.web;\n\npublic final class Paths {\n"
            + "".join(f"    static final String {line};\n" for line in constants)
            + "}\n"
        ),
        f"{JAVA}/web/Api.java": (
            "package org.acme.shop.web;\n\nimport org.springframework.web.bind.annotation.*;\n\n"
            f"@RestController\npublic class Api {{\n{methods}}}\n"
        ),
    }
    root = tmp_path / str(len(list(tmp_path.iterdir())))
    build, _ = _build(root, files)
    return sorted(key.removeprefix("api_endpoint GET ") for key in _endpoints(build))


def test_a_constant_has_one_value_whichever_route_reads_it_first(tmp_path: Path) -> None:
    """A constant is worked out from its own declaration: ``L2`` is a route after ``L5`` read it
    on the way as before; one built more than MAX_DEPTH constants deep, or from itself, is no
    route; a chain of thousands, or a diamond of them doubling, costs one pass."""
    chain = ['L0 = "/api"', *(f'L{i} = L{i - 1} + "/s{i}"' for i in range(1, 12))]
    both = ["/api/s1/s2", "/api/s1/s2/s3/s4/s5"]
    assert _routes_of(tmp_path, chain, ["L5", "L2"]) == both
    assert _routes_of(tmp_path, chain, ["L2", "L5"]) == both
    assert _routes_of(tmp_path, chain, ["L7", "L8"]) == ["/api/s1/s2/s3/s4/s5/s6/s7"]
    cycle = ['A = B + "/x"', 'B = A + "/y"', 'C = "/c"']
    assert _routes_of(tmp_path, cycle, ["A", "B", "C"]) == ["/c"]
    started = time.monotonic()
    long = ['K0 = "/k"', *(f"K{i} = K{i - 1}" for i in range(1, 5000))]
    assert _routes_of(tmp_path, long, ["K4999", "K3"]) == ["/k"]
    diamond = ['D0 = ""', *(f"D{i} = D{i - 1} + D{i - 1}" for i in range(1, 60))]
    assert _routes_of(tmp_path, diamond, ["D59"]) == []
    assert time.monotonic() - started < 10
