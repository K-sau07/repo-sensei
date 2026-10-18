"""Tracing (OpenTelemetry) and error reporting (Sentry).

Both are optional and configured from settings:

* spans are exported over OTLP/HTTP when ``SENSEI_OTEL_ENDPOINT`` is set, or printed to stderr
  when ``SENSEI_OTEL_CONSOLE=true``; otherwise tracing is a no-op;
* Sentry is enabled when ``SENSEI_SENTRY_DSN`` is set. Every event passes through the same secret
  redaction as the logs, and personally identifying data is never attached by default.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from sqlalchemy.ext.asyncio import AsyncEngine

from sensei_core.config import Settings
from sensei_core.redaction import redact

_state: dict[str, bool] = {"tracing": False, "sentry": False}


def build_tracer_provider(settings: Settings, service_name: str) -> TracerProvider:
    from sensei_core import __version__  # noqa: PLC0415 - avoid import cycle at module load

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": __version__,
            "deployment.environment": settings.env,
        }
    )
    provider = TracerProvider(resource=resource)
    processors: list[SpanProcessor] = []
    if settings.telemetry.otel_endpoint:
        endpoint = settings.telemetry.otel_endpoint.rstrip("/") + "/v1/traces"
        processors.append(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    if settings.telemetry.otel_console:
        processors.append(BatchSpanProcessor(ConsoleSpanExporter()))
    for processor in processors:
        provider.add_span_processor(processor)
    return provider


def init_tracing(
    settings: Settings, service_name: str, *, provider: TracerProvider | None = None
) -> bool:
    """Install the global tracer provider and client instrumentations. Idempotent.

    Returns True if tracing was initialised by this call.
    """
    if _state["tracing"]:
        return False
    tracing_enabled = (
        provider is not None
        or bool(settings.telemetry.otel_endpoint)
        or settings.telemetry.otel_console
    )
    if not tracing_enabled:
        return False
    trace.set_tracer_provider(provider or build_tracer_provider(settings, service_name))
    HTTPXClientInstrumentor().instrument()  # pyright: ignore[reportUnknownMemberType]
    RedisInstrumentor().instrument()  # pyright: ignore[reportUnknownMemberType]
    _state["tracing"] = True
    return True


def instrument_engine(engine: AsyncEngine) -> None:
    """Trace SQL statements of an async engine (no-op when tracing is disabled)."""
    if _state["tracing"]:
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)  # pyright: ignore[reportUnknownMemberType]


def sentry_before_send(event: Any, _hint: Any) -> Any:
    """Redact secrets from a Sentry event before it leaves the process."""
    return redact(event)


def init_sentry(settings: Settings, service_name: str) -> bool:
    """Enable Sentry error reporting when a DSN is configured. Idempotent."""
    dsn = settings.telemetry.sentry_dsn
    if _state["sentry"] or dsn is None:
        return False
    sentry_sdk.init(
        dsn=dsn.get_secret_value(),
        environment=settings.env,
        server_name=service_name,
        send_default_pii=False,
        traces_sample_rate=settings.telemetry.traces_sample_rate,
        before_send=sentry_before_send,
    )
    _state["sentry"] = True
    return True


def init_telemetry(settings: Settings, service_name: str) -> None:
    init_tracing(settings, service_name)
    init_sentry(settings, service_name)


def tag_request(request_id: str) -> None:
    """Attach the request id to the active span and to Sentry's scope."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("request.id", request_id)
    sentry_sdk.set_tag("request_id", request_id)
