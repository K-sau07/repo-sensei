"""Webhook intake. Verified deliveries are stored once and processed by workers."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response, status
from pydantic import BaseModel

from sensei_api.deps import DbSession
from sensei_api.errors import AppError
from sensei_core.config import Settings
from sensei_core.github.webhooks import WebhookDispatcher, record_delivery, signature_valid
from sensei_core.logging import get_logger

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger(__name__)


class WebhookAccepted(BaseModel):
    status: Literal["queued", "duplicate"]


def get_dispatcher(request: Request) -> WebhookDispatcher:
    dispatcher: WebhookDispatcher = request.app.state.webhook_dispatcher
    return dispatcher


async def _read_body(request: Request, limit: int) -> bytes:
    """The raw body (the signature covers exact bytes), refusing anything over ``limit``."""
    too_large = AppError("Payload too large.", code="payload_too_large", status_code=413)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/github",
    response_model=WebhookAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"model": WebhookAccepted, "description": "Already received"}},
)
async def github_webhook(
    *,
    request: Request,
    response: Response,
    db: DbSession,
    background: BackgroundTasks,
    dispatcher: Annotated[WebhookDispatcher, Depends(get_dispatcher)],
    x_github_event: Annotated[str | None, Header(max_length=64)] = None,
    x_github_delivery: Annotated[str | None, Header(max_length=64)] = None,
    x_hub_signature_256: Annotated[str | None, Header(max_length=128)] = None,
) -> WebhookAccepted:
    """Receive a GitHub delivery: verify its signature, store it once, queue it for a worker."""
    settings: Settings = request.app.state.settings
    secret = settings.github.webhook_secret
    if secret is None:
        raise AppError(
            "Webhooks are not configured.", code="webhooks_not_configured", status_code=503
        )
    body = await _read_body(request, settings.github.webhook_max_bytes)
    if not signature_valid(secret.get_secret_value(), body, x_hub_signature_256):
        raise AppError("Invalid signature.", code="invalid_signature", status_code=401)
    if not x_github_event or not x_github_delivery:
        raise AppError("Missing GitHub delivery headers.", code="invalid_delivery")
    try:
        parsed: object = json.loads(body)
    except ValueError:
        raise AppError("The payload is not JSON.", code="invalid_delivery") from None
    if not isinstance(parsed, dict):
        raise AppError("The payload is not a JSON object.", code="invalid_delivery")
    payload = cast("dict[str, Any]", parsed)

    stored: uuid.UUID | None = await record_delivery(
        db,
        delivery_id=x_github_delivery,
        event=x_github_event,
        payload=payload,
        contains_nul=b"\\u0000" in body,
    )
    if stored is None:
        log.info(
            "duplicate webhook ignored", delivery=x_github_delivery, github_event=x_github_event
        )
        response.status_code = status.HTTP_200_OK
        return WebhookAccepted(status="duplicate")
    # Runs after the response, so after the insert has committed: a worker always finds the row.
    background.add_task(dispatcher.dispatch, stored)
    log.info("webhook received", delivery=x_github_delivery, github_event=x_github_event)
    return WebhookAccepted(status="queued")
