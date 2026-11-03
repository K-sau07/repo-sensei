"""Sending what's pending (8.10): each delivery (an email, a Slack post) is claimed, sent and
recorded in a transaction of its own (``FOR UPDATE SKIP LOCKED``: two workers never send the
same one, and one that was sent isn't rolled back into pending by a later one's failure).

A send that failed in a way that could pass (a timeout, a 5xx, a 429) is tried again on the
next round, up to ``max_attempts``; one that can't (a 4xx, a Slack webhook that's no longer
valid) fails at once. One with nothing to send it (no email provider, the org's Slack removed or
no longer posting that kind) is skipped; so is one pending for over a day (it's news no more).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.logging import get_logger
from sensei_core.models import (
    DeliveryChannel,
    DeliveryState,
    NotificationDelivery,
    SlackIntegration,
)
from sensei_core.notifications.senders import (
    Email,
    Emailer,
    SendError,
    SlackPoster,
    slack_escape,
)
from sensei_core.security.crypto import DecryptionError, TokenCipher

log = get_logger(__name__)

BATCH = 50
STALE = timedelta(days=1)


def slack_context(org_id: uuid.UUID) -> str:
    """What an org's Slack webhook URL is encrypted with (it opens for that org only)."""
    return f"slack:{org_id}"


def app_link(origin: str, link: str | None) -> str | None:
    """A notification's link, absolute (only paths in the app: anything else is dropped)."""
    if not link or not link.startswith("/") or link.startswith("//"):
        return None
    return origin.rstrip("/") + link


def email_text(delivery: NotificationDelivery, origin: str) -> str:
    parts = [delivery.subject]
    if delivery.text:
        parts.append(delivery.text)
    link = app_link(origin, delivery.link)
    if link:
        parts.append(f"Open it: {link}")
    settings = app_link(origin, "/settings/notifications")
    parts.append(f"-- repo-sensei. Choose what you're emailed about: {settings}")
    return "\n\n".join(parts)


def slack_text(delivery: NotificationDelivery, origin: str) -> str:
    lines = [f"*{slack_escape(delivery.subject)}*"]
    if delivery.text:
        lines.append(slack_escape(delivery.text))
    link = app_link(origin, delivery.link)
    if link:
        lines.append(f"<{link}|Open in repo-sensei>")
    return "\n".join(lines)


@dataclass(slots=True)
class Delivered:
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class Senders:
    emailer: Emailer | None
    slack: SlackPoster
    cipher: TokenCipher
    origin: str  # the web app's, for links
    max_attempts: int = 5


async def deliver_pending(
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    senders: Senders,
    *,
    limit: int = BATCH,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Delivered:
    """Send up to ``limit`` pending deliveries (``sessions``: the system database's, as the
    worker reads every org's)."""
    done = Delivered()
    tried: set[uuid.UUID] = set()  # each at most once a round (a retry waits for the next)
    for _ in range(limit):
        async with sessions() as db:
            pending = select(NotificationDelivery).where(
                NotificationDelivery.state == DeliveryState.PENDING
            )
            if tried:
                pending = pending.where(NotificationDelivery.id.not_in(tried))
            delivery = await db.scalar(
                pending.order_by(NotificationDelivery.attempts, NotificationDelivery.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if delivery is None:
                break
            tried.add(delivery.id)
            await _deliver(db, delivery, senders, clock(), done)
    if done.sent or done.failed or done.retrying:
        log.info("notifications delivered", sent=done.sent, retrying=done.retrying,
                 failed=done.failed, skipped=done.skipped)  # fmt: skip
    return done


async def _deliver(
    db: AsyncSession,
    delivery: NotificationDelivery,
    senders: Senders,
    now: datetime,
    done: Delivered,
) -> None:
    if now - delivery.created_at > STALE:
        _end(delivery, DeliveryState.SKIPPED, "pending for over a day", done)
        return
    try:
        if delivery.channel is DeliveryChannel.EMAIL:
            if senders.emailer is None:
                _end(delivery, DeliveryState.SKIPPED, "no email provider configured", done)
                return
            email = Email(
                delivery.recipient, delivery.subject, email_text(delivery, senders.origin)
            )
            await senders.emailer.send(email, key=f"{delivery.key}:{delivery.recipient}")
        else:
            slack = await db.scalar(
                select(SlackIntegration).where(SlackIntegration.org_id == delivery.org_id)
            )
            if slack is None or delivery.kind.value not in (slack.kinds or []):
                _end(delivery, DeliveryState.SKIPPED, "the org's Slack doesn't post this", done)
                return
            url = senders.cipher.decrypt_text(
                slack.webhook_url_enc, context=slack_context(delivery.org_id)
            )
            await senders.slack.post(url, slack_text(delivery, senders.origin))
    except SendError as exc:
        delivery.attempts += 1
        delivery.error = str(exc)[:500]
        if exc.retry and delivery.attempts < senders.max_attempts:
            done.retrying += 1
        else:
            delivery.state = DeliveryState.FAILED
            done.failed += 1
            log.warning("notification not delivered", channel=delivery.channel.value,
                        key=delivery.key, error=delivery.error)  # fmt: skip
        return
    except DecryptionError:
        delivery.attempts += 1
        _end(delivery, DeliveryState.FAILED, "the org's Slack webhook couldn't be read", done)
        return
    delivery.attempts += 1
    delivery.state = DeliveryState.SENT
    delivery.error = None
    delivery.sent_at = now
    done.sent += 1


def _end(delivery: NotificationDelivery, state: DeliveryState, why: str, done: Delivered) -> None:
    delivery.state = state
    delivery.error = why
    if state is DeliveryState.SKIPPED:
        done.skipped += 1
    else:
        done.failed += 1
