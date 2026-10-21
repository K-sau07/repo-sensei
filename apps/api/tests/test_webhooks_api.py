"""Webhook intake over HTTP: signatures, redeliveries, size limits and dispatch."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from sensei_core.config import GitHubSettings, Settings
from sensei_core.github.webhooks import sign

pytestmark = pytest.mark.integration

URL = "/api/v1/webhooks/github"
SECRET = "whsec-test"


class Capture:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    async def dispatch(self, delivery_id: uuid.UUID) -> None:
        self.dispatched.append(delivery_id)


@pytest.fixture
def dispatcher(api_app: FastAPI, migrated_test_db: Settings) -> Capture:
    github = GitHubSettings(webhook_secret=SecretStr(SECRET), webhook_max_bytes=4096)
    api_app.state.settings = migrated_test_db.model_copy(update={"github": github})
    capture = Capture()
    api_app.state.webhook_dispatcher = capture
    return capture


def _headers(body: bytes, delivery: str, event: str = "installation") -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": sign(SECRET, body),
        "Content-Type": "application/json",
    }


def _body(**extra: Any) -> bytes:
    return json.dumps({"action": "created", "installation": {"id": 1}, **extra}).encode()


async def test_verified_delivery_is_queued(
    api_client: httpx.AsyncClient, dispatcher: Capture
) -> None:
    body = _body()
    response = await api_client.post(URL, content=body, headers=_headers(body, "d-1"))
    assert response.status_code == 202
    assert response.json() == {"status": "queued"}
    assert len(dispatcher.dispatched) == 1


async def test_redelivery_is_acknowledged_without_work(
    api_client: httpx.AsyncClient, dispatcher: Capture
) -> None:
    body = _body()
    first = await api_client.post(URL, content=body, headers=_headers(body, "d-2"))
    replay = await api_client.post(URL, content=body, headers=_headers(body, "d-2"))
    assert (first.status_code, replay.status_code) == (202, 200)
    assert replay.json() == {"status": "duplicate"}
    assert len(dispatcher.dispatched) == 1


@pytest.mark.parametrize(
    "signature",
    [None, "", "sha256=" + "0" * 64, sign("another secret", _body()), "sha1=abc"],
)
async def test_bad_signatures_are_unauthorized(
    signature: str | None, api_client: httpx.AsyncClient, dispatcher: Capture
) -> None:
    body = _body()
    headers = _headers(body, "d-3")
    if signature is None:
        del headers["X-Hub-Signature-256"]
    else:
        headers["X-Hub-Signature-256"] = signature
    response = await api_client.post(URL, content=body, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"
    assert dispatcher.dispatched == []
    # Nothing was stored: the genuine delivery with the same id is still accepted.
    genuine = await api_client.post(URL, content=body, headers=_headers(body, "d-3"))
    assert genuine.status_code == 202


async def test_signature_covers_the_exact_bytes(
    api_client: httpx.AsyncClient, dispatcher: Capture
) -> None:
    body = _body()
    headers = _headers(body, "d-4")
    reformatted = json.dumps(json.loads(body), indent=2).encode()  # same JSON, other bytes
    response = await api_client.post(URL, content=reformatted, headers=headers)
    assert response.status_code == 401


async def test_oversized_payloads_are_refused(
    api_client: httpx.AsyncClient, dispatcher: Capture
) -> None:
    body = _body(padding="x" * 5000)
    declared = await api_client.post(URL, content=body, headers=_headers(body, "d-5"))
    assert declared.status_code == 413
    assert declared.json()["error"]["code"] == "payload_too_large"

    async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: the stream is counted
        for start in range(0, len(body), 1000):
            yield body[start : start + 1000]

    streamed = await api_client.post(URL, content=chunks(), headers=_headers(body, "d-6"))
    assert streamed.status_code == 413
    assert dispatcher.dispatched == []


@pytest.mark.parametrize(
    ("body", "drop", "code"),
    [
        (b"not json", None, "invalid_delivery"),
        (b"[1, 2, 3]", None, "invalid_delivery"),
        (_body(), "X-GitHub-Event", "invalid_delivery"),
        (_body(), "X-GitHub-Delivery", "invalid_delivery"),
    ],
)
async def test_malformed_deliveries_are_bad_requests(
    body: bytes,
    drop: str | None,
    code: str,
    api_client: httpx.AsyncClient,
    dispatcher: Capture,
) -> None:
    headers = _headers(body, "d-7")
    if drop:
        del headers[drop]
    response = await api_client.post(URL, content=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert dispatcher.dispatched == []


async def test_unconfigured_webhooks_are_unavailable(api_client: httpx.AsyncClient) -> None:
    body = _body()
    response = await api_client.post(URL, content=body, headers=_headers(body, "d-8"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "webhooks_not_configured"


async def test_payloads_with_nul_characters_are_stored(
    api_client: httpx.AsyncClient, dispatcher: Capture
) -> None:
    body = _body(head_commit={"message": "fix \u0000 bug"})
    assert b"\\u0000" in body
    response = await api_client.post(URL, content=body, headers=_headers(body, "d-9", "push"))
    assert response.status_code == 202
