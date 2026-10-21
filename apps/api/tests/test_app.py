from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from sensei_api import __version__
from sensei_api.errors import NotFound

API = "/api/v1"


class EchoPayload(BaseModel):
    name: str
    count: int


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_request_id_is_generated(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/healthz")
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 36  # uuid7 string


async def test_safe_incoming_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/healthz", headers={"X-Request-Id": "abc-123.DEF_4"})
    assert response.headers["x-request-id"] == "abc-123.DEF_4"


async def test_unsafe_incoming_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/healthz", headers={"X-Request-Id": "bad id <script>"})
    assert response.headers["x-request-id"] != "bad id <script>"


async def test_unknown_route_uses_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["x-request-id"]


async def test_validation_errors_use_envelope(app: FastAPI, client: httpx.AsyncClient) -> None:
    @app.post(f"{API}/_test/echo")
    async def echo(payload: EchoPayload) -> EchoPayload:  # pyright: ignore[reportUnusedFunction]
        return payload

    response = await client.post(f"{API}/_test/echo", json={"name": "x", "count": "many"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["details"][0]["loc"] == ["body", "count"]


async def test_app_errors_use_envelope(app: FastAPI, client: httpx.AsyncClient) -> None:
    @app.get(f"{API}/_test/missing")
    async def missing() -> None:  # pyright: ignore[reportUnusedFunction]
        raise NotFound("Repo not found.", code="repo_not_found_or_no_access")

    response = await client.get(f"{API}/_test/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "repo_not_found_or_no_access"


async def test_unhandled_errors_return_500_envelope_without_trace(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    @app.get(f"{API}/_test/boom")
    async def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        msg = "secret internal detail"
        raise RuntimeError(msg)

    response = await client.get(f"{API}/_test/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert "secret internal detail" not in response.text


async def test_readyz_reports_checks(app: FastAPI, client: httpx.AsyncClient) -> None:
    async def ok() -> None:
        return None

    async def broken() -> None:
        raise ConnectionError

    app.state.readiness_checks = {"ok": ok}
    response = await client.get(f"{API}/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"ok": "ok"}}

    app.state.readiness_checks = {"ok": ok, "db": broken}
    response = await client.get(f"{API}/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["db"] == "error: ConnectionError"


async def test_large_json_is_gzipped_but_sse_is_not(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    @app.get(f"{API}/_test/big")
    async def big() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"data": "x" * 5000}

    @app.get(f"{API}/_test/stream", response_class=EventSourceResponse)
    async def stream() -> AsyncIterator[ServerSentEvent]:  # pyright: ignore[reportUnusedFunction]
        for i in range(3):
            yield ServerSentEvent(data={"n": i, "pad": "y" * 1000})

    big_response = await client.get(f"{API}/_test/big", headers={"Accept-Encoding": "gzip"})
    assert big_response.headers.get("content-encoding") == "gzip"

    sse_response = await client.get(f"{API}/_test/stream", headers={"Accept-Encoding": "gzip"})
    assert sse_response.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in sse_response.headers
    assert sse_response.text.count("data:") == 3


def test_openapi_export_is_stable(tmp_path: Path) -> None:
    import importlib.util  # noqa: PLC0415 - script module loaded by path

    script = Path(__file__).resolve().parents[3] / "scripts" / "export_openapi.py"
    spec = importlib.util.spec_from_file_location("export_openapi", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    first = module.export(tmp_path / "a.json").read_text()
    second = module.export(tmp_path / "b.json").read_text()
    assert first == second
    paths = json.loads(first)["paths"]
    assert f"{API}/healthz" in paths
    assert f"{API}/readyz" in paths


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_docs_only_exposed_in_dev(env: str) -> None:
    from sensei_api.app import create_app  # noqa: PLC0415
    from sensei_core.config import Settings  # noqa: PLC0415
    from sensei_testing import prod_settings  # noqa: PLC0415

    app = create_app(Settings(env="dev") if env == "dev" else prod_settings())
    assert (app.docs_url is not None) is (env == "dev")


async def test_http_exception_with_non_string_detail(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    from fastapi import HTTPException  # noqa: PLC0415

    @app.get(f"{API}/_test/odd-detail")
    async def odd() -> None:  # pyright: ignore[reportUnusedFunction]
        raise HTTPException(status_code=503, detail={"upstream": "down"})

    response = await client.get(f"{API}/_test/odd-detail")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "dependency_unavailable"
    assert isinstance(error["message"], str)


async def test_dev_500s_carry_cors_headers() -> None:
    from sensei_api.app import create_app  # noqa: PLC0415
    from sensei_core.config import Settings, WebSettings  # noqa: PLC0415

    app = create_app(Settings(env="dev", web=WebSettings(origin="http://localhost:3000")))

    @app.get(f"{API}/_test/boom-cors")
    async def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        msg = "kaboom"
        raise RuntimeError(msg)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"{API}/_test/boom-cors", headers={"Origin": "http://localhost:3000"}
        )
    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "x-request-id" in response.headers


async def test_indexing_stages_are_published(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/indexing/stages")).json()
    stages = [entry["stage"] for entry in body["stages"]]
    assert stages[0] == "clone"
    assert stages[-1] == "finalize"
    assert sum(entry["weight"] for entry in body["stages"]) == 100
    unlocks = {entry["stage"]: entry["unlocks"] for entry in body["stages"]}
    assert unlocks["embeddings"] == ["semantic_search", "ask"]
