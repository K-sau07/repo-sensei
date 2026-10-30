"""Frontend HTTP calls: every detector and URL form on a small repository, then the
fastapi-template's generated client."""

from __future__ import annotations

import tarfile
import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest

from sensei_core.models import EdgeType
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.writer import LINKS
from sensei_engine.link.http_calls import CallGraph, client_calls, env_values, scan_all
from sensei_engine.parse.parser import ParseResult, parse_file
from sensei_engine.symbols.extract import extract_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"

REPO = {
    "web/tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}}}',
    "web/.env": "VITE_API_URL=http://localhost:8000/backend\n",
    "web/src/config.ts": """
        export const API = "/api"
        export const ROUTES = { users: "/api/users", items: `${API}/items` }
    """,
    "web/src/http.ts": """
        import axios from "axios"
        import { API } from "./config"

        export const api = axios.create({ baseURL: `${API}/v2` })

        export function getJson(path: string) {
          return fetch(API + path).then((r) => r.json())
        }

        export function request(method: string, path: string) {
          return getJson(path)
        }
    """,
    "web/src/components/Users.tsx": """
        import { useQuery } from "@tanstack/react-query"
        import useSWR from "swr"
        import ky from "ky"
        import createClient from "openapi-fetch"
        import { api, getJson } from "@/http"
        import { ROUTES } from "../config"

        const client = createClient({ baseUrl: "" })

        export function Users({ id }: { id: number }) {
          const { data } = useQuery({
            queryKey: ["users"],
            queryFn: () => fetch(ROUTES.users, { method: "GET" }),
          })
          const url = `/api/users/${id}/avatar`
          fetch(url, { method: "PUT" })
          useSWR(`/api/users/${id}`, fetcher)
          ky.post("/api/ky")
          api.delete(`/things/${id}`)
          client.GET("/api/users/{id}", { params: { path: { id } } })
          getJson("/wrapped")
          window.fetch(import.meta.env.VITE_API_URL + "/health")
          fetch(process.env.NEXT_PUBLIC_UNKNOWN + "/x")
          const params = new URLSearchParams()
          params.get("/not-a-call")
          const cache = new Map()
          cache.get("/neither")
          fetch(someUrl)
          return null
        }
    """,
    "web/src/hooks/useItems.ts": """
        import axios from "axios"
        import { ROUTES } from "@/config"

        export function useItems() {
          return axios({ url: ROUTES.items, method: "post" })
        }
    """,
    "server/index.js": """
        import express from "express"
        const app = express()
        app.get("/api/users", (req, res) => res.send([]))
        fetch("/api/from-server")
    """,
}


def _graph(files: Mapping[str, str]) -> CallGraph:
    definitions: dict[str, list[Definition]] = {}
    results: list[tuple[ParseResult, list[Definition]]] = []
    for path, content in sorted(files.items()):
        if not path.endswith((".ts", ".tsx", ".js", ".jsx")):
            continue
        parsed = parse_file(path, content.encode())
        assert parsed.language is not None
        definitions[path] = [
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, parsed.language)
        ]
        results.append((parsed, definitions[path]))
    configs = {p: c for p, c in files.items() if is_config(p)}
    project = Project(frozenset(files), configs, definitions)
    envs = env_values(project, {p: c for p, c in files.items() if "/.env" in f"/{p}"})
    return client_calls(project, scan_all(results, envs))


@pytest.fixture(scope="module")
def repo() -> CallGraph:
    return _graph({path: textwrap.dedent(text).lstrip() for path, text in REPO.items()})


def test_every_detector_and_url_form(repo: CallGraph) -> None:
    found = {(c.method, c.url, c.base, c.dynamic) for c in repo.calls}
    assert found == {
        ("GET", "/api/users", "", False),  # useQuery's queryFn: an imported object's property
        ("PUT", "/api/users/{}/avatar", "", False),  # a local const, a template literal
        ("GET", "/api/users/{}", "", False),  # useSWR's key
        ("POST", "/api/ky", "", False),
        ("DELETE", "/things/{}", "/api/v2", False),  # an imported axios instance's baseURL
        ("GET", "/api/users/{id}", "", False),  # openapi-fetch
        ("GET", "/api/wrapped", "", False),  # a wrapper: fetch(API + path)
        ("GET", "http://localhost:8000/backend/health", "", False),  # .env value
        ("GET", "{env:NEXT_PUBLIC_UNKNOWN}/x", "", False),  # unknown env: kept as a segment
        ("GET", "", "", True),  # fetch(someUrl): nothing known
        ("POST", "/api/items", "", False),  # axios(config) through a tsconfig alias
    }
    assert repo.report.through_wrappers == 1
    assert not [c for c in repo.calls if "not-a-call" in c.url or "neither" in c.url]
    assert not [c for c in repo.calls if (c.evidence_path or "").startswith("server/")]


def test_calls_are_issued_by_the_definition_around_them(repo: CallGraph) -> None:
    LINKS.check(repo.nodes, repo.edges)
    issuers = {(e.src, e.dst.split(" ")[1]) for e in repo.edges}
    assert all(e.type is EdgeType.ISSUES_REQUEST for e in repo.edges)
    assert ("tsx web/src/components/Users.tsx/Users().", "PUT") in issuers
    assert ("ts web/src/hooks/useItems.ts/useItems().", "POST") in issuers
    node = next(n for n in repo.nodes if n.attrs["client"] == "axios" and n.attrs["base_ref"])
    assert node.attrs["url_template"] == "/things/{}"
    assert node.attrs["base_ref"] == "/api/v2"
    assert node.key.startswith("http_client_call DELETE /things/{} web/src/components/Users.tsx:")


def test_a_wrapper_calling_a_wrapper_is_followed() -> None:
    graph = _graph(
        {
            "a.ts": textwrap.dedent(
                """
                const BASE = "/api"
                function raw(p: string) { return fetch(BASE + p) }
                function mid(p: string) { return raw("/v1" + p) }
                export function top() { return mid("/deep") }
                """
            )
        }
    )
    assert [c.url for c in graph.calls] == ["/api/v1/deep"]


# URL helpers, as Langflow builds its URLs: a base read from the page at run time, a helper
# returning it, and one looking a path up by key, extending it per key of an object.
HELPERS = {
    "web/src/config.ts": """
        const documentBase = document.querySelector("base")?.getAttribute("href")
        export const BASENAME = documentBase?.startsWith("/") ? documentBase : ""
        export const BASE_URL_API = `${BASENAME}/api/v1/`
        export const BASE_URL_API_V2 = `${BASENAME}/api/v2/`
    """,
    "web/src/urls.ts": """
        import { BASE_URL_API } from "./config"

        export function getBaseUrl(): string {
          return BASE_URL_API || "/api/v1/"
        }
    """,
    "web/src/constants.ts": """
        import { BASE_URL_API_V2 } from "./config"
        import { getBaseUrl } from "./urls"

        export const URLs = { API_KEY: `api_key`, FLOWS: "flows", MIGRATION: "migration" }

        export function getURL(
          key: keyof typeof URLs,
          params: Record<string, unknown> = {},
          v2: boolean = false,
        ) {
          let url = URLs[key]
          for (const paramKey of Object.keys(params)) {
            url += `/${params[paramKey]}`
          }
          return `${v2 ? BASE_URL_API_V2 : getBaseUrl()}${url}`
        }

        export const flowUrl = (id: string) => `${getURL("FLOWS")}/${id}`
    """,
    "web/src/api.ts": """
        import axios from "axios"
        export const api = axios.create({})
    """,
    "web/src/queries.ts": """
        import { api } from "./api"
        import * as constants from "./constants"
        import { getURL } from "./constants"
        import { getBaseUrl } from "./urls"

        export async function createApiKey(name: string) {
          return api.post(`${getBaseUrl()}api_key/`, { name })
        }
        export function deleteKey(key: { id: string }) {
          return api.delete(`${getURL("API_KEY")}/${key.id}`)
        }
        export const listV2 = () => api.get(getURL("FLOWS", {}, true))
        export const pause = () => api.post(getURL("MIGRATION", { path: "pause" }))
        export const one = (id: string) => api.get(constants.flowUrl(id))
        export const some = (params: object) => api.get(getURL("FLOWS", params))
        export const nope = () => api.get(getURL("NOPE"))
    """,
}


def test_a_url_helper_is_evaluated_where_it_is_called() -> None:
    """Each call's URL is the helper's value with its arguments: the base it can't know a hole
    in front (an unknown base, 3.10), the key looked up, the flag's branch, the object's values
    appended; what depends on an argument it can't know is unknown, not wrong."""
    graph = _graph({path: textwrap.dedent(text).lstrip() for path, text in HELPERS.items()})
    found = {(c.method, c.url, c.dynamic) for c in graph.calls}
    assert found == {
        ("POST", "{}/api/v1/api_key/", False),
        ("DELETE", "{}/api/v1/api_key/{}", False),
        ("GET", "{}/api/v2/flows", False),  # v2: true
        ("POST", "{}/api/v1/migration/pause", False),  # { path: "pause" } appended
        ("GET", "{}/api/v1/flows/{}", False),  # a helper calling a helper, through a namespace
        ("GET", "{}", True),  # params: how many keys, and which, isn't known
        ("GET", "{}", True),  # no such key
    }


def test_the_options_of_a_call_may_be_a_constant() -> None:
    graph = _graph(
        {
            "a.js": textwrap.dedent(
                """
                const shared = { method: "DELETE" }
                export function run() {
                  const id = window.flowId
                  const options = { method: "POST", headers: {} }
                  fetch(`/api/v1/run/${id}`, options)
                  fetch("/api/v1/items", shared)
                }
                """
            )
        }
    )
    assert {(c.method, c.url) for c in graph.calls} == {
        ("POST", "/api/v1/run/{}"),
        ("DELETE", "/api/v1/items"),
    }


def test_an_awaited_call_with_type_arguments_is_a_call() -> None:
    graph = _graph(
        {
            "a.ts": textwrap.dedent(
                """
                import axios from "axios"
                const api = axios.create({ baseURL: "/api" })
                export async function load() {
                  const items = await api.get<Item[]>("/items")
                  const one = await fetch<Item>("/one")
                  return [items, one]
                }
                """
            )
        }
    )
    assert {(c.method, c.url, c.base) for c in graph.calls} == {
        ("GET", "/items", "/api"),
        ("GET", "/one", ""),
    }


def test_the_template_generated_client(tmp_path: Path) -> None:
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    base = tmp_path / "fastapi-template"
    files = {
        str(p.relative_to(base)): p.read_text(errors="replace")
        for p in base.rglob("*")
        if p.is_file()
        and "node_modules" not in p.parts
        and (p.suffix in (".ts", ".tsx") or is_config(p.name) or p.name.startswith(".env"))
        and str(p.relative_to(base)).startswith("frontend/")
    }
    graph = _graph(files)
    app = [c for c in graph.calls if (c.evidence_path or "").startswith("frontend/src/")]
    assert len(app) == 23  # one per SDK method (hey-api, config objects)
    assert {c.evidence_path for c in app} == {"frontend/src/client/sdk.gen.ts"}
    assert not [c for c in graph.calls if c.dynamic]
    # The rest: the end-to-end tests' calls to the mail catcher.
    assert {c.evidence_path for c in graph.calls} - {c.evidence_path for c in app} == {
        "frontend/tests/utils/mailpit.ts"
    }
    issuers = {e.src for e in graph.edges}
    assert "ts frontend/src/client/sdk.gen.ts/ItemsService#readItems()." in issuers


def test_aliases_come_from_real_tsconfigs() -> None:
    """A URL in a string isn't a comment, nor is a glob's ``/*`` one's start, and ``paths`` can
    come through ``extends``: the tsconfigs generators write."""
    graph = _graph(
        {
            "web/tsconfig.base.json": textwrap.dedent(
                """
                {
                  "$schema": "https://json.schemastore.org/tsconfig",
                  // where things are
                  "compilerOptions": {
                    "baseUrl": ".",
                    "paths": { "@/*": ["./src/*"] },
                  },
                  "include": ["src/**/*.ts", "src/**/*.tsx"]
                }
                """
            ),
            "web/tsconfig.json": '{ "extends": "./tsconfig.base.json", /* app */ }',
            "web/src/lib/api.ts": 'export const USERS = "/api/users"\n',
            "web/src/app.ts": textwrap.dedent(
                """
                import { USERS } from "@/lib/api"
                export function load() { return fetch(USERS) }
                """
            ),
        }
    )
    assert [c.url for c in graph.calls] == ["/api/users"]


def test_a_query_string_chosen_in_the_url_is_no_path() -> None:
    """`${base}/${qs ? `?${qs}` : ""}`: a query string or none is not a path segment; a
    ternary of two paths is a hole unless both are the same."""
    graph = _graph(
        {
            "a.ts": textwrap.dedent(
                """
                import axios from "axios"
                const api = axios.create({ baseURL: "/api" })
                export async function add(qs: string, archived: boolean) {
                  await api.post(`/variables/${qs ? `?${qs}` : ""}`, {})
                  await api.get(`/items${archived ? "#old" : ""}`)
                  await api.get(`/items/${archived ? "old" : "new"}`)
                  await api.get(`/same/${archived ? "x" : "x"}`)
                }
                """
            )
        }
    )
    assert {(c.method, c.url) for c in graph.calls} == {
        ("POST", "/variables/"),
        ("GET", "/items"),
        ("GET", "/items/{}"),
        ("GET", "/same/x"),
    }


def test_a_helper_that_hands_its_url_on_is_followed_through_a_default_export() -> None:
    """``qs ? `${url}?${qs}` : url`` hands the URL it is given on, a query string appended at
    most; it is found through ``export default`` and through a function that calls it."""
    graph = _graph(
        {
            "web/src/query.ts": textwrap.dedent(
                """
                const withQuery = (url: string, params: object): string => {
                  const qs = new URLSearchParams(params as any).toString()
                  return qs ? `${url}?${qs}` : url
                }
                export default withQuery
                """
            ),
            "web/src/invoices.ts": textwrap.dedent(
                """
                import axios from "axios"
                import withQuery from "./query"
                const api = axios.create({ baseURL: "/api" })
                const paged = (url: string, params: object): string => withQuery(url, params)
                export async function invoices(id: string, params: object) {
                  await api.get(paged(`/invoices/${id}`, params))
                  await api.get(withQuery("/accounts", params))
                }
                """
            ),
        }
    )
    assert {(c.method, c.url) for c in graph.calls} == {
        ("GET", "/invoices/{}"),
        ("GET", "/accounts"),
    }
