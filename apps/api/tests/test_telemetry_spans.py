"""Spans are emitted for requests and carry the request id.

Installing a tracer provider is global for the process, so this module installs an in-memory
provider once and leaves it in place (it only records spans in memory).
"""

from __future__ import annotations

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sensei_api.app import create_app
from sensei_core.config import Settings
from sensei_core.telemetry import init_tracing
from sensei_testing import settings_for_tests

exporter = InMemorySpanExporter()


async def test_request_span_has_request_id() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    init_tracing(Settings(env="test"), "sensei-api-test", provider=provider)

    app = create_app(Settings(env="test"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/healthz", headers={"X-Request-Id": "trace-me-1"})
    assert response.status_code == 200

    server_spans = [s for s in exporter.get_finished_spans() if s.kind.name == "SERVER"]
    assert server_spans, "expected a server span for the request"
    assert server_spans[-1].attributes is not None
    assert server_spans[-1].attributes.get("request.id") == "trace-me-1"


@pytest.mark.integration
async def test_database_and_redis_calls_are_traced() -> None:
    """Guards against instrumentations silently disabling themselves (e.g. version ranges)."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    init_tracing(Settings(env="test"), "sensei-api-test", provider=provider)
    exporter.clear()

    app = create_app(settings_for_tests())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/readyz")
    assert response.status_code == 200

    names = {span.name for span in exporter.get_finished_spans()}
    assert "SELECT" in names, names
    assert "PING" in names, names
