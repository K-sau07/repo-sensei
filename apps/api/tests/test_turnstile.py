"""Cloudflare Turnstile (10.2): a good token passes, a bad one doesn't, and a check that can't
be made lets nothing through."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from sensei_api.turnstile import (
    MAX_TOKEN_CHARS,
    HumanCheckFailedError,
    HumanCheckUnavailableError,
    verify,
)

SECRET = SecretStr("s")
PASSED = {"success": True, "hostname": "app.example.com", "action": "ask"}


def _client(*replies: httpx.Response | Exception) -> tuple[httpx.AsyncClient, list[str]]:
    keys: list[str] = []
    queue = list(replies)

    def answer(request: httpx.Request) -> httpx.Response:
        keys.append(dict(httpx.QueryParams(request.content.decode()))["idempotency_key"])
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return httpx.AsyncClient(transport=httpx.MockTransport(answer)), keys


async def test_a_timeout_is_asked_again_with_the_same_key() -> None:
    http, keys = _client(httpx.ReadTimeout("slow"), httpx.Response(200, json=PASSED))
    await verify(http, SECRET, "token", None, hostname="app.example.com")
    assert len(keys) == 2
    assert keys[0] == keys[1]  # Cloudflare answers the first try's verdict


async def test_what_cloudflare_refuses_or_cant_say_lets_nothing_through() -> None:
    http, _ = _client(httpx.Response(200, json={"success": False, "error-codes": ["bad"]}))
    with pytest.raises(HumanCheckFailedError):
        await verify(http, SECRET, "token", "203.0.113.9")
    http, _ = _client(httpx.Response(500), httpx.Response(200, text="not json"))
    with pytest.raises(HumanCheckUnavailableError):
        await verify(http, SECRET, "token", None)
    http, keys = _client()
    with pytest.raises(HumanCheckFailedError):
        await verify(http, SECRET, "x" * (MAX_TOKEN_CHARS + 1), None)
    with pytest.raises(HumanCheckUnavailableError):
        await verify(http, SecretStr(""), "token", None)
    assert keys == []  # neither was sent


async def test_a_check_passed_for_another_page_or_site_is_refused() -> None:
    for said in ({**PASSED, "hostname": "evil.example"}, {**PASSED, "action": "login"}):
        http, _ = _client(httpx.Response(200, json=said))
        with pytest.raises(HumanCheckFailedError):
            await verify(http, SECRET, "token", None, hostname="App.Example.com")
    # Cloudflare's test secret answers for any page (development, tests).
    http, _ = _client(httpx.Response(200, json={"success": True, "hostname": "example.com"}))
    await verify(http, SecretStr("1x0000000000000000000000000000000AA"), "t", None, hostname="x")
