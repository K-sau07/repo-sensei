"""Liveness and readiness endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sensei_api import __version__

router = APIRouter(tags=["health"])

ReadinessCheck = Callable[[], Awaitable[None]]
"""An async callable that raises if its dependency is not ready."""

CHECK_TIMEOUT_SECONDS = 2.0


async def _run_check(check: ReadinessCheck) -> str:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await check()
    except TimeoutError:
        return "error: timeout"
    except Exception as exc:
        return f"error: {type(exc).__name__}"
    return "ok"


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
    outcomes = await asyncio.gather(*(_run_check(check) for check in checks.values()))
    results = dict(zip(checks, outcomes, strict=True))
    # No registered checks means the dependencies were never wired up: not ready.
    healthy = bool(results) and all(value == "ok" for value in results.values())
    body = ReadinessResponse(status="ok" if healthy else "unavailable", checks=results)
    return JSONResponse(body.model_dump(), status_code=200 if healthy else 503)
