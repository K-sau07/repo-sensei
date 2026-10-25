"""A fake Anthropic API for tests: an httpx2 mock transport that answers messages, token counts
and batches from canned responses, and keeps every request it saw."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from pydantic import SecretStr

from sensei_core.config import AnthropicSettings
from sensei_engine.llm.anthropic_client import AnthropicClient

BASE_URL = "https://api.anthropic.test"


def settings(**overrides: Any) -> AnthropicSettings:
    values: dict[str, Any] = {
        "api_key": SecretStr("sk-ant-test"),
        "base_url": BASE_URL,
        "max_retries": 0,
        **overrides,
    }
    return AnthropicSettings(**values)


def message(
    text: str,
    *,
    model: str = "claude-sonnet-5-5",
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage
        or {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def batch(batch_id: str, status: str = "in_progress", **counts: int) -> dict[str, Any]:
    ended = status == "ended"
    return {
        "id": batch_id,
        "type": "message_batch",
        "processing_status": status,
        "request_counts": {
            "processing": 0 if ended else sum(counts.values()) or 1,
            "succeeded": counts.get("succeeded", 0),
            "errored": counts.get("errored", 0),
            "canceled": counts.get("canceled", 0),
            "expired": counts.get("expired", 0),
        },
        "created_at": "2026-10-24T00:00:00Z",
        "expires_at": "2026-10-25T00:00:00Z",
        "ended_at": "2026-10-24T00:30:00Z" if ended else None,
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": f"{BASE_URL}/v1/messages/batches/{batch_id}/results" if ended else None,
    }


Handler = Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]


@dataclass
class FakeAnthropic:
    """Answers each path from ``routes`` (a response, or a function of the request) and keeps
    every request it saw."""

    routes: dict[str, httpx2.Response | Handler] = field(
        default_factory=dict[str, httpx2.Response | Handler]
    )
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx2.Response(
                404,
                json={
                    "type": "error",
                    "error": {"type": "not_found_error", "message": request.url.path},
                },
            )
        if not callable(route):
            return route
        answer = route(request)
        return await answer if inspect.isawaitable(answer) else answer

    def http(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def client(self, **overrides: Any) -> AnthropicClient:
        http = self.http()
        return AnthropicClient(settings(**overrides), http_client=http)

    def bodies(self, path: str) -> list[dict[str, Any]]:
        """The JSON bodies sent to ``path`` (requests without one, such as a GET, left out)."""
        return [json.loads(r.content) for r in self.requests if r.url.path == path and r.content]


def jsonl(*lines: dict[str, Any]) -> httpx2.Response:
    content = "\n".join(json.dumps(line) for line in lines).encode()
    return httpx2.Response(200, content=content, headers={"content-type": "application/binary"})
