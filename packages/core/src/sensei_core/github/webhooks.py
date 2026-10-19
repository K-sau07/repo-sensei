"""GitHub webhooks: verify, store once, then process in a worker.

Intake (the API) is small on purpose: check the HMAC signature over the raw body, insert the
delivery (the unique ``delivery_id`` turns GitHub's redeliveries into no-ops) and hand its id to
the dispatcher after the insert commits. Everything else happens in :func:`process_delivery`,
which a worker runs with the system role (deliveries are not org scoped until we know which org an
installation belongs to).

Deliveries are a durable inbox: one that is stored but never dispatched (the queue was down) stays
``received`` and is picked up again by a periodic sweep.
"""

from __future__ import annotations

import hashlib
import hmac
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.db import rowcount
from sensei_core.db.base import new_id
from sensei_core.github.app import forget_installation_tokens
from sensei_core.github.client import as_object
from sensei_core.github.installations import installation_details
from sensei_core.logging import get_logger
from sensei_core.models import ActorType, DeliveryStatus, GitHubInstallation, WebhookDelivery

log = get_logger(__name__)

PROVIDER = "github"
SIGNATURE_PREFIX = "sha256="
MAX_ERROR_LENGTH = 4000


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body)."""
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode())


def sign(secret: str, body: bytes) -> str:
    """The signature GitHub would send for ``body`` (tests and local tools)."""
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _without_nul(value: Any) -> Any:
    """Postgres JSONB cannot store U+0000, which GitHub payloads (commit messages) can hold."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, list):
        return [_without_nul(item) for item in cast("list[Any]", value)]
    if isinstance(value, dict):
        return {k: _without_nul(v) for k, v in cast("dict[str, Any]", value).items()}
    return value


async def record_delivery(
    db: AsyncSession,
    *,
    delivery_id: str,
    event: str,
    payload: dict[str, Any],
    contains_nul: bool = False,
) -> uuid.UUID | None:
    """Store a verified delivery; None if this ``delivery_id`` was already stored.

    Works with the insert-only app role: no RETURNING and no conflict target (both need SELECT).
    """
    action = payload.get("action")
    installation = as_object(payload.get("installation")) or {}
    installation_id = installation.get("id")
    row_id = new_id()
    result = await db.execute(
        pg_insert(WebhookDelivery)
        .values(
            id=row_id,
            provider=PROVIDER,
            delivery_id=delivery_id[:64],
            event=event[:64],
            action=action[:64] if isinstance(action, str) else None,
            installation_id=installation_id if isinstance(installation_id, int) else None,
            payload=_without_nul(payload) if contains_nul else payload,
        )
        .on_conflict_do_nothing()
    )
    return row_id if rowcount(result) == 1 else None


class WebhookDispatcher(Protocol):
    """Hands a stored delivery to a worker."""

    async def dispatch(self, delivery_id: uuid.UUID) -> None: ...


async def stale_deliveries(
    db: AsyncSession, *, older_than: timedelta, limit: int
) -> list[uuid.UUID]:
    """Deliveries still waiting for a worker after ``older_than``, oldest first."""
    rows = await db.scalars(
        select(WebhookDelivery.id)
        .where(
            WebhookDelivery.status == DeliveryStatus.RECEIVED,
            WebhookDelivery.received_at < _now() - older_than,
        )
        .order_by(WebhookDelivery.received_at)
        .limit(limit)
    )
    return list(rows)


# ---------------------------------------------------------------------- processing


@dataclass(frozen=True, slots=True)
class HandlerContext:
    db: AsyncSession  # system role: installations of every org are visible
    redis: Redis
    delivery: WebhookDelivery

    @property
    def payload(self) -> dict[str, Any]:
        return self.delivery.payload

    async def installation(self) -> GitHubInstallation | None:
        if self.delivery.installation_id is None:
            return None
        return await self.db.scalar(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == self.delivery.installation_id)
            .with_for_update()
        )

    async def audit(self, installation: GitHubInstallation, action: AuditAction) -> None:
        sender = as_object(self.payload.get("sender")) or {}
        await audit(
            self.db,
            action,
            actor_user_id=None,
            actor_type=ActorType.SYSTEM,
            org_id=installation.org_id,
            target_type="github_installation",
            target_id=installation.id,
            meta={
                "installation_id": installation.installation_id,
                "account_login": installation.account_login,
                "github_sender": sender.get("login"),
                "delivery_id": self.delivery.delivery_id,
            },
        )


Handler = Callable[[HandlerContext], Awaitable[DeliveryStatus]]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    status: DeliveryStatus | None  # None: missing, or another worker holds it
    error: BaseException | None = None


def _now() -> datetime:
    return datetime.now(UTC)


async def _installation_refreshed(ctx: HandlerContext) -> DeliveryStatus:
    """``installation.created``: a link from the Setup URL may already exist; refresh it.
    Unlinked installations are only stored: we can't know their org until an admin links them."""
    row = await ctx.installation()
    body = as_object(ctx.payload.get("installation"))
    if row is not None and body is not None:
        for name, value in installation_details(body).items():
            setattr(row, name, value)
        row.deleted_at = None
    return DeliveryStatus.PROCESSED


async def _installation_deleted(ctx: HandlerContext) -> DeliveryStatus:
    row = await ctx.installation()
    if row is not None and row.deleted_at is None:
        row.deleted_at = _now()
        await ctx.audit(row, AuditAction.GITHUB_INSTALL_DELETED)
    if ctx.delivery.installation_id is not None:
        await forget_installation_tokens(ctx.redis, ctx.delivery.installation_id)
    return DeliveryStatus.PROCESSED


async def _installation_suspended(ctx: HandlerContext) -> DeliveryStatus:
    row = await ctx.installation()
    body = as_object(ctx.payload.get("installation")) or {}
    if row is not None and row.suspended_at is None:
        row.suspended_at = installation_details(body)["suspended_at"] or _now()
        await ctx.audit(row, AuditAction.GITHUB_INSTALL_SUSPENDED)
    if ctx.delivery.installation_id is not None:
        await forget_installation_tokens(ctx.redis, ctx.delivery.installation_id)
    return DeliveryStatus.PROCESSED


async def _installation_unsuspended(ctx: HandlerContext) -> DeliveryStatus:
    row = await ctx.installation()
    if row is not None and row.suspended_at is not None:
        row.suspended_at = None
        await ctx.audit(row, AuditAction.GITHUB_INSTALL_UNSUSPENDED)
    return DeliveryStatus.PROCESSED


async def _permissions_accepted(ctx: HandlerContext) -> DeliveryStatus:
    row = await ctx.installation()
    body = as_object(ctx.payload.get("installation"))
    if row is not None and body is not None:
        details = installation_details(body)
        row.permissions = details["permissions"]
        row.events = details["events"]
        await ctx.audit(row, AuditAction.GITHUB_INSTALL_PERMISSIONS_ACCEPTED)
    if ctx.delivery.installation_id is not None:
        # Cached tokens carry the old permissions.
        await forget_installation_tokens(ctx.redis, ctx.delivery.installation_id)
    return DeliveryStatus.PROCESSED


async def _repositories_changed(ctx: HandlerContext) -> DeliveryStatus:
    """``installation_repositories.added|removed``: the account changed which repositories the
    app may read. The selection mode is kept here; repositories themselves sync with repos."""
    row = await ctx.installation()
    selection = ctx.payload.get("repository_selection")
    if row is not None and isinstance(selection, str):
        row.repository_selection = selection[:16]
    if ctx.delivery.installation_id is not None:
        await forget_installation_tokens(ctx.redis, ctx.delivery.installation_id)
    return DeliveryStatus.PROCESSED


async def _store_only(_: HandlerContext) -> DeliveryStatus:
    """Kept for later phases (pushes drive freshness; pull requests drive reviews)."""
    return DeliveryStatus.STORED


# (event, action): handler; an action of None matches every action of that event.
HANDLERS: dict[tuple[str, str | None], Handler] = {
    ("installation", "created"): _installation_refreshed,
    ("installation", "deleted"): _installation_deleted,
    ("installation", "suspend"): _installation_suspended,
    ("installation", "unsuspend"): _installation_unsuspended,
    ("installation", "new_permissions_accepted"): _permissions_accepted,
    ("installation_repositories", "added"): _repositories_changed,
    ("installation_repositories", "removed"): _repositories_changed,
    ("push", None): _store_only,
    ("pull_request", None): _store_only,
}

_FINAL = frozenset({DeliveryStatus.PROCESSED, DeliveryStatus.STORED, DeliveryStatus.IGNORED})


def handler_for(event: str, action: str | None) -> Handler | None:
    return HANDLERS.get((event, action)) or HANDLERS.get((event, None))


async def process_delivery(db: AsyncSession, redis: Redis, delivery_id: uuid.UUID) -> ProcessResult:
    """Run the handler for one delivery, at most once to completion.

    The row is locked (``SKIP LOCKED``: a concurrent worker skips it), finished deliveries are
    left alone, and a failing handler is rolled back to a savepoint and recorded as ``failed`` with
    the error returned, so the caller can commit the record and then decide whether to retry.
    """
    delivery = await db.scalar(
        select(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id)
        .with_for_update(skip_locked=True)
    )
    if delivery is None:
        return ProcessResult(None)
    if delivery.status in _FINAL:
        return ProcessResult(delivery.status)

    delivery.attempts += 1
    handler = handler_for(delivery.event, delivery.action)
    error: BaseException | None = None
    if handler is None:
        status = DeliveryStatus.IGNORED
    else:
        try:
            async with db.begin_nested():
                status = await handler(HandlerContext(db=db, redis=redis, delivery=delivery))
        except Exception as exc:
            status, error = DeliveryStatus.FAILED, exc
            delivery.error = "".join(traceback.format_exception_only(exc)).strip()[
                :MAX_ERROR_LENGTH
            ]
            log.warning(
                "webhook handler failed",
                delivery=delivery.delivery_id,
                github_event=delivery.event,
                error_type=type(exc).__name__,
            )
    delivery.status = status
    if status is not DeliveryStatus.FAILED:
        delivery.error = None
        delivery.processed_at = _now()
    await db.flush()
    return ProcessResult(status, error)
