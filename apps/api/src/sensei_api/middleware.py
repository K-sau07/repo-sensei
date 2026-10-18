"""ASGI middleware.

Written as plain ASGI (not ``BaseHTTPMiddleware``) so streaming responses such as SSE are never
buffered.
"""

from __future__ import annotations

import json
import re

import structlog
import uuid_utils
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sensei_api.errors import error_body
from sensei_core.logging import get_logger

log = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def new_request_id() -> str:
    return str(uuid_utils.uuid7())


class RequestIdMiddleware:
    """Accept a safe incoming ``X-Request-Id`` or create one, bind it to the log context and echo
    it on the response. Also turns unexpected exceptions into the JSON error envelope, so even a
    500 carries the request id."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = ""
        for name, value in scope["headers"]:
            if name.decode("latin-1").lower() == REQUEST_ID_HEADER:
                incoming = value.decode("latin-1")
                break
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else new_request_id()

        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:
                log.exception("unhandled error", error_type=type(exc).__name__)
                if response_started:
                    raise
                body = json.dumps(
                    error_body("internal_error", "Something went wrong on our side.")
                ).encode()
                await send_with_id(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
