"""Which address counts as the client's (audit log, sessions)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from sensei_api.middleware import RequestIdMiddleware
from sensei_core.context import current_client_ip


def _app(header: str | None) -> FastAPI:
    app = FastAPI()

    @app.get("/ip")
    async def ip() -> dict[str, str | None]:
        return {"ip": current_client_ip()}

    app.add_middleware(RequestIdMiddleware, client_ip_header=header)
    return app


async def _ip(app: FastAPI, **headers: str) -> str | None:
    transport = httpx.ASGITransport(app=app, client=("10.0.0.7", 51000))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        body: dict[str, str | None] = (await client.get("/ip", headers=headers)).json()
        return body["ip"]


async def test_forwarded_headers_are_not_trusted_by_default() -> None:
    assert await _ip(_app(None), **{"X-Forwarded-For": "1.2.3.4", "Fly-Client-IP": "1.2.3.4"}) == (
        "10.0.0.7"
    )


async def test_the_platform_header_is_used_when_configured() -> None:
    app = _app("Fly-Client-IP")
    assert await _ip(app, **{"Fly-Client-IP": "203.0.113.9"}) == "203.0.113.9"
    assert await _ip(app, **{"Fly-Client-IP": "2001:db8::1"}) == "2001:db8::1"


@pytest.mark.parametrize("value", ["", "not-an-ip", "1.2.3.4, 5.6.7.8"])
async def test_a_malformed_header_falls_back_to_the_peer(value: str) -> None:
    assert await _ip(_app("fly-client-ip"), **{"Fly-Client-IP": value}) == "10.0.0.7"


async def test_only_the_configured_header_counts() -> None:
    app = _app("fly-client-ip")
    assert await _ip(app, **{"X-Forwarded-For": "1.2.3.4"}) == "10.0.0.7"
