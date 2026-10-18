"""Liveness and readiness endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sensei_api import __version__

router = APIRouter(tags=["health"])

ReadinessCheck = Callable[[], Awaitable[None]]
"""An async callable that raises if its dependency is not ready."""


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, str]


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness: the process is up and serving requests."""
    return HealthResponse(status="ok", version=__version__)


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readyz(request: Request) -> JSONResponse:
    """Readiness: every registered dependency check passes."""
    checks: dict[str, ReadinessCheck] = request.app.state.readiness_checks
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            await check()
            results[name] = "ok"
        except Exception as exc:
            results[name] = f"error: {type(exc).__name__}"
    healthy = all(value == "ok" for value in results.values())
    body = ReadinessResponse(status="ok" if healthy else "unavailable", checks=results)
    return JSONResponse(body.model_dump(), status_code=200 if healthy else 503)
