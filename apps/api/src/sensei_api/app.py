"""FastAPI application factory.

Run locally with ``make api`` (uvicorn ``--factory``). All routes live under ``/api/v1``; the web
app reaches them same-origin through a rewrite, so cookies work without CORS in every
environment except direct local calls during development.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.middleware.gzip import GZipMiddleware

from sensei_api import __version__
from sensei_api.errors import install_error_handlers
from sensei_api.middleware import RequestIdMiddleware
from sensei_api.routers import auth, github, health, invitations, me, orgs, webhooks
from sensei_api.routers.health import ReadinessCheck
from sensei_core.auth.sessions import SessionService
from sensei_core.config import Settings, get_settings
from sensei_core.db import make_database
from sensei_core.jobs import CeleryDispatcher, make_celery
from sensei_core.logging import configure_logging, get_logger
from sensei_core.mail import ConsoleMailer
from sensei_core.orgs.service import OrgService
from sensei_core.redis import make_redis, ping_redis
from sensei_core.security.crypto import TokenCipher
from sensei_core.telemetry import init_telemetry, instrument_engine, scrub_server_span

API_PREFIX = "/api/v1"
SERVICE_NAME = "sensei-api"

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    init_telemetry(settings, SERVICE_NAME)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        database = make_database(settings.database)
        instrument_engine(database.engine)
        redis = make_redis(settings.redis)
        http = httpx.AsyncClient(timeout=settings.github.timeout_seconds)
        app.state.database = database
        app.state.redis = redis
        app.state.http = http
        app.state.readiness_checks.update(
            {"database": database.ping, "redis": lambda: ping_redis(redis)}
        )
        log.info("api starting", env=settings.env, version=__version__)
        try:
            yield
        finally:
            # Close each resource independently so one failure cannot leak the other.
            try:
                await http.aclose()
            finally:
                try:
                    await redis.aclose()
                finally:
                    await database.dispose()
            log.info("api stopped")

    app = FastAPI(
        title="repo-sensei API",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs" if settings.is_dev else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_service = SessionService(settings.auth)
    app.state.token_cipher = TokenCipher.from_settings(settings.auth)
    app.state.org_service = OrgService(ConsoleMailer(), settings.web.origin)
    # Sends tasks by name; the workers package defines them.
    app.state.webhook_dispatcher = CeleryDispatcher(make_celery(settings, main="sensei-api"))
    readiness_checks: dict[str, ReadinessCheck] = {}
    app.state.readiness_checks = readiness_checks

    install_error_handlers(app)

    # Order matters: the last added middleware runs first (outermost).
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestIdMiddleware, client_ip_header=settings.api.client_ip_header)
    if settings.is_dev:
        # Outermost in dev so every response, including 500s built by RequestIdMiddleware,
        # carries CORS headers when the API is called directly from the dev web origin.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.web.origin],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["x-request-id"],
        )

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(me.router, prefix=API_PREFIX)
    app.include_router(orgs.router, prefix=API_PREFIX)
    app.include_router(invitations.router, prefix=API_PREFIX)
    app.include_router(github.router, prefix=API_PREFIX)
    app.include_router(webhooks.router, prefix=API_PREFIX)
    # Wraps the whole middleware stack; RequestIdMiddleware tags the server span it creates.
    FastAPIInstrumentor.instrument_app(app, server_request_hook=scrub_server_span)
    return app
