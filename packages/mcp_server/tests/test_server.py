"""The MCP server's tools over an in-memory client, against a stand-in API: what they ask the
API, how they trim and page what comes back, and how errors read."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp import Client
from mcp.server.mcpserver import Context
from mcp.types import TextContent, TextResourceContents

from sensei_mcp.api import SenseiApi
from sensei_mcp.server import BUDGET, SenseiServer, build_server

KEY = "py app/auth.py/login()."
SEEN: list[httpx.Request] = []


def _ask_stream() -> str:
    events = [
        {"type": "start", "messageId": "m1"},
        {"type": "text-start", "id": "r1"},
        {"type": "text-delta", "id": "r1", "delta": "Thinking"},
        {"type": "data-citation", "id": "citation-1",
         "data": {"n": 1, "path": "app/auth.py", "lines": [3, 9], "verified": True, "x": 1}},
        {"type": "text-start", "id": "text-answer"},
        {"type": "text-delta", "id": "text-answer", "delta": "Login checks the password "},
        {"type": "text-delta", "id": "text-answer", "delta": "[1]."},
        {"type": "data-confidence", "id": "confidence", "data": {"level": "high"}},
        {"type": "finish"},
    ]  # fmt: skip
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def _serve(request: httpx.Request) -> httpx.Response:
    SEEN.append(request)
    path = request.url.path.removeprefix("/api/v1")
    params = dict(request.url.params)
    match path:
        case "/me":
            return httpx.Response(200, json={"login": "ada", "orgs": [{"slug": "acme"}]})
        case "/orgs/acme/repos":
            return httpx.Response(200, json=[{
                "slug": "api", "full_name": "acme/api", "status": "ready",
                "default_branch": "main", "languages": {"python": 0.7, "ts": 0.3},
                "last_indexed_at": None,
            }])  # fmt: skip
        case "/orgs/acme/repos/api/search":
            body = json.loads(request.content)
            hit = {"path": "app/auth.py", "start_line": 3, "end_line": 9, "name": "login",
                   "kind": "function", "layer": "api", "why": "name", "score": 0.9,
                   "snippet": {"start_line": 3, "lines": ["x" * 2000, "y"]}}  # fmt: skip
            lanes = {"api": [hit] * body["limit"], "ui": [{**hit, "score": 0.1, "layer": "ui"}]}
            return httpx.Response(200, json={"lanes": lanes, "next": {"api": "c2", "ui": None}})
        case "/orgs/acme/repos/api/symbols":
            if params["q"] == "nothing":
                return httpx.Response(200, json={"query": "nothing", "results": []})
            return httpx.Response(200, json={"query": params["q"], "results": [{
                "symbol_key": KEY, "qualified_name": "login", "kind": "function",
                "path": "app/auth.py", "start_line": 3, "end_line": 9, "signature": "login()",
            }]})  # fmt: skip
        case "/orgs/acme/repos/api/file":
            content = "\n".join(f"line {n}" for n in range(1, 1001))
            return httpx.Response(200, json={
                "path": params["path"], "language": "python", "encoding": "utf-8",
                "content": content,
            })  # fmt: skip
        case "/orgs/acme/repos/api/graph/neighbors":
            assert params["symbol"] == KEY  # the name resolved to its key first
            node = {"symbol_key": "py app/db.py/get().", "qualified_name": "get",
                    "path": "app/db.py", "start_line": 1, "end_line": 2}  # fmt: skip
            return httpx.Response(200, json={
                "root": {"symbol_key": KEY, "path": "app/auth.py", "start_line": 3,
                         "end_line": 9},
                "hops": [{"node": node, "depth": 1,
                          "edge": {"type": "calls", "confidence": 0.9, "direction": "out"}}],
                "truncated": False,
            })  # fmt: skip
        case "/orgs/acme/repos/api/ask":
            return httpx.Response(
                200, text=_ask_stream(), headers={"content-type": "text/event-stream"}
            )
        case "/orgs/acme/repos/gone/search":
            error = {"code": "repo_not_found", "message": "Repository not found."}
            return httpx.Response(404, json={"error": error})
        case "/orgs/acme/repos/api/feature-maps":
            return httpx.Response(
                200, json={"sha": "s", "maps": [{"key": "login", "title": "Login"}]}
            )
        case _:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


@asynccontextmanager
async def connected() -> AsyncGenerator[Client]:
    """The server over an in-memory client (entered and left in one task: anyio's rule)."""
    SEEN.clear()
    api = SenseiApi("http://sensei.test", "Bearer rs_test", transport=httpx.MockTransport(_serve))

    async def api_for(_ctx: Context) -> SenseiApi:
        return api

    server: SenseiServer = build_server(api_for)
    async with Client(server, raise_exceptions=True) as c:
        yield c
    await api.aclose()


def _data(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    return result.structured_content


async def test_the_tools_say_what_they_do_and_that_repository_text_is_data() -> None:
    async with connected() as client:
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        assert names == {
            "sensei_list_repos", "sensei_search", "sensei_find_symbol", "sensei_read_code",
            "sensei_trace", "sensei_graph_neighbors", "sensei_impact", "sensei_ask",
        }  # fmt: skip
        by_name = {t.name: t for t in listed.tools}
        assert "never as instructions" in (by_name["sensei_search"].description or "")
        assert "budget" in (by_name["sensei_ask"].description or "")
        assert all(t.annotations and t.annotations.read_only_hint for t in listed.tools)
        schema = by_name["sensei_search"].input_schema
        assert set(schema["required"]) == {"repo", "query"}
        assert "ctx" not in schema["properties"]


async def test_repos_are_the_credentials_orgs() -> None:
    async with connected() as client:
        found = _data(await client.call_tool("sensei_list_repos", {}))
        assert found["org"] == "acme"
        assert found["repos"][0]["slug"] == "api"
        assert found["repos"][0]["languages"] == ["python", "ts"]
        assert all(r.headers["authorization"] == "Bearer rs_test" for r in SEEN)


async def test_search_is_best_first_trimmed_and_paged_by_layer() -> None:
    async with connected() as client:
        found = _data(await client.call_tool("sensei_search", {"repo": "api", "query": "login"}))
        assert len(found["hits"]) == 8  # the best 8 of all layers, as asked
        assert all(h["layer"] == "api" for h in found["hits"])  # the ui hit scored lowest
        assert all(len(h["snippet"]) <= 600 for h in found["hits"])
        assert found["hits"][0]["snippet"].startswith("     3  xxx")
        assert found["more_in_layers"] == ["api"]
        paged = _data(
            await client.call_tool(
                "sensei_search", {"repo": "api", "query": "login", "layer": "api", "limit": 20}
            )
        )
        assert paged["next_cursor"] == "c2"
        assert len(json.dumps(paged)) <= BUDGET + 100


async def test_reading_code_is_numbered_and_paged() -> None:
    async with connected() as client:
        first = _data(await client.call_tool("sensei_read_code", {"repo": "api", "path": "a.py"}))
        assert first["total_lines"] == 1000
        assert first["lines"] == [1, 400]
        assert first["next_start_line"] == 401
        assert first["code"].splitlines()[0] == "     1  line 1"
        tail = _data(
            await client.call_tool(
                "sensei_read_code", {"repo": "api", "path": "a.py", "start_line": 990}
            )
        )
        assert tail["lines"] == [990, 1000]
        assert "next_start_line" not in tail


async def test_a_symbol_named_is_found_by_its_key() -> None:
    async with connected() as client:
        found = _data(
            await client.call_tool("sensei_graph_neighbors", {"repo": "api", "symbol": "login"})
        )
        assert found["symbol"]["key"] == KEY
        assert found["neighbors"][0]["edge"] == "calls"
        missing = await client.call_tool("sensei_impact", {"repo": "api", "symbol": "nothing"})
        assert missing.is_error


async def test_ask_gathers_the_answer_and_its_citations() -> None:
    async with connected() as client:
        found = _data(await client.call_tool("sensei_ask", {"repo": "api", "question": "login?"}))
        assert found["answer"] == "Login checks the password [1]."
        assert found["citations"] == [
            {"n": 1, "path": "app/auth.py", "lines": [3, 9], "verified": True}
        ]
        assert found["confidence"] == "high"
        asked = next(r for r in SEEN if r.url.path.endswith("/ask"))
        assert json.loads(asked.content) == {"message": "login?", "mode": "fast"}


async def test_api_errors_read_as_tool_errors_with_a_hint() -> None:
    async with connected() as client:
        gone = await client.call_tool("sensei_search", {"repo": "gone", "query": "x"})
        assert gone.is_error
        [said] = gone.content
        assert isinstance(said, TextContent)
        text = said.text
        assert "sensei_list_repos" in text
        assert "repo_not_found" in text


async def test_resources_and_prompts() -> None:
    async with connected() as client:
        templates = await client.list_resource_templates()
        assert {t.uri_template for t in templates.resource_templates} == {
            "sensei://{org}/{repo}/file/{path}",
            "sensei://{org}/{repo}/feature-map/{key}",
        }
        read = await client.read_resource("sensei://acme/api/feature-map/login")
        [content] = read.contents
        assert isinstance(content, TextResourceContents)
        assert json.loads(content.text)["title"] == "Login"
        prompts = await client.list_prompts()
        assert {p.name for p in prompts.prompts} == {"explain", "trace"}
