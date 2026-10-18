"""Application errors and the single JSON error envelope.

Every error response has the shape::

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {...}}}

``code`` values are stable, machine-readable identifiers listed in docs/build/90-reference.md.
Stack traces are never returned to clients; unexpected errors are logged with full detail.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sensei_core.context import current_context

_HTTP_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_failed",
    429: "rate_limited",
}


class AppError(Exception):
    """Base class for expected, client-facing errors."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.details = details
        self.status_code = status_code or self.status_code


class Unauthorized(AppError):
    status_code = 401
    code = "unauthenticated"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class CostCapReached(AppError):
    status_code = 429
    code = "cost_cap_reached"


class DependencyUnavailable(AppError):
    status_code = 503
    code = "dependency_unavailable"


def error_body(
    code: str, message: str, details: dict[str, Any] | list[Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": current_context().get("request_id"),
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


async def _app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # registered only for AppError
        raise exc
    return JSONResponse(error_body(exc.code, exc.message, exc.details), status_code=exc.status_code)


async def _validation_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # registered only for RequestValidationError
        raise exc
    details = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return JSONResponse(
        error_body("validation_failed", "Request validation failed.", details), status_code=422
    )


async def _http_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # registered only for StarletteHTTPException
        raise exc
    code = _HTTP_STATUS_CODES.get(exc.status_code, "http_error")
    message = exc.detail or code.replace("_", " ")
    return JSONResponse(error_body(code, message), status_code=exc.status_code, headers=exc.headers)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    # Unexpected exceptions are turned into the envelope by RequestIdMiddleware, which runs inside
    # the request-id context (Starlette's catch-all handler runs outside user middleware).
