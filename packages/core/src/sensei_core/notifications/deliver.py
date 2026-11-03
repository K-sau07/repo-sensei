"""Sending what's pending (8.10): each delivery (an email, a Slack post) is claimed, sent and
recorded in a transaction of its own (``FOR UPDATE SKIP LOCKED``: two workers never send the
same one, and one that was sent isn't rolled back into pending by a later one's failure).

A send that failed in a way that could pass (a timeout, a 5xx, a 429, GitHub not answering
whether its person may read the repository) is tried again later, each wait twice the last (a
minute, two, four...), up to ``max_attempts``; one that can't (a 4xx, a Slack webhook that's no
longer valid) fails at once. Anything else that goes wrong counts as a try too: one bad delivery
never stops the others. One with nothing to send it (no email provider, the org's Slack removed
or no longer posting that kind) is skipped; so is one pending for over a day (it's news no more).

An email about a repository goes only to someone who can still read it on GitHub (asked with
their own token, as everywhere else); someone who can't isn't sent it. When their GitHub
sign-in has lapsed (nobody can tell), the email says only what kind of thing happened, in which
org, and links to their inbox there: the app shows the rest to whoever may see it.

A Slack post is sent at most once per try, but a worker that stops between Slack's answer and
the commit sends it again on the next round (Slack's webhooks have no idempotency key; emails
have Resend's).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.github.client import GitHubError
from sensei_core.logging import get_logger
from sensei_core.models import (
    DeliveryChannel,
    DeliveryState,
    NotificationDelivery,
    NotificationKind,
    Org,
    Repo,
    SlackIntegration,
    User,
)
from sensei_core.notifications.senders import (
    Email,
    Emailer,
    SendError,
    SlackPoster,
    slack_escape,
)
from sensei_core.repos.access import open_to_all
from sensei_core.security.crypto import DecryptionError, TokenCipher

log = get_logger(__name__)

BATCH = 50
STALE = timedelta(days=1)
FIRST_WAIT = timedelta(minutes=1)  # before the first retry; each after waits twice the last

# What an email says when whether its person may read the repository can't be told.
WITHHELD: dict[NotificationKind, str] = {
    NotificationKind.APPROVAL_NEEDED: "A plan is waiting for approval",
    NotificationKind.PR_OPENED: "A draft pull request was opened",
    NotificationKind.FIX_FAILED: "A fix or change stopped",
    NotificationKind.INDEX_DONE: "A repository is indexed",
    NotificationKind.INDEX_FAILED: "A repository's indexing failed",
    NotificationKind.COST_CAP: "Agent spend is near today's cap",
}

# Whether a person may read a repository on GitHub: None when it can't be told (their GitHub
# sign-in must be renewed). Raises GitHubError when GitHub doesn't answer.
CanRead = Callable[[User, Repo], Awaitable[bool | None]]


def slack_context(org_id: uuid.UUID) -> str:
    """What an org's Slack webhook URL is encrypted with (it opens for that org only)."""
    return f"slack:{org_id}"


def app_link(origin: str, link: str | None) -> str | None:
    """A notification's link, absolute (only paths in the app: anything else is dropped)."""
    if not link or not link.startswith("/") or link.startswith("//"):
        return None
    # A backslash is read as "/" by browsers ("/\\evil.example"), and controls are dropped.
    if any(char == "\\" or ord(char) < 0x20 or ord(char) == 0x7F for char in link):
        return None
    return origin.rstrip("/") + link


@dataclass(frozen=True, slots=True)
class Withheld:
    """An email's words held back: said is only the org it happened in."""

    org_name: str
    org_slug: str


def email_text(
    delivery: NotificationDelivery, origin: str, *, withheld: Withheld | None = None
) -> str:
    """The email's words (``withheld``: only the kind of thing and the org, linking to the
    person's inbox there, for one whose access couldn't be checked)."""
    if withheld is not None:
        parts = [f"{WITHHELD[delivery.kind]} in {withheld.org_name}. Sign in to see it."]
        link = app_link(origin, f"/{quote(withheld.org_slug, safe='')}/inbox")
    else:
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
    max_attempts: int = 8
    can_read: CanRead | None = None  # None: nothing to ask GitHub with (emails withheld)


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
            now = clock()
            pending = select(NotificationDelivery).where(
                NotificationDelivery.state == DeliveryState.PENDING,
                or_(
                    NotificationDelivery.next_attempt_at.is_(None),
                    NotificationDelivery.next_attempt_at <= now,
                ),
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
            try:
                async with db.begin_nested():  # what it did undone, the row still held
                    await _deliver(db, delivery, senders, now, done)
            except Exception as exc:  # one bad delivery mustn't stop the others
                await db.refresh(delivery)
                log.warning("notification delivery broke", key=delivery.key,
                            error=type(exc).__name__)  # fmt: skip
                why = f"unexpected {type(exc).__name__}"
                _tried(delivery, why, retry=True, now=now, senders=senders, done=done)
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
            skipped = await _email(db, delivery, senders)
        else:
            skipped = await _slack(db, delivery, senders)
    except SendError as exc:
        _tried(delivery, str(exc), retry=exc.retry, now=now, senders=senders, done=done)
        return
    except GitHubError as exc:
        why = f"GitHub: {exc.status or 'no answer'}"
        _tried(delivery, why, retry=True, now=now, senders=senders, done=done)
        return
    except DecryptionError:
        delivery.attempts += 1
        _end(delivery, DeliveryState.FAILED, "the org's Slack webhook couldn't be read", done)
        return
    if skipped is not None:
        _end(delivery, DeliveryState.SKIPPED, skipped, done)
        return
    delivery.attempts += 1
    delivery.state = DeliveryState.SENT
    delivery.error = None
    delivery.sent_at = now
    done.sent += 1


async def _email(db: AsyncSession, delivery: NotificationDelivery, senders: Senders) -> str | None:
    """Send the email (None), or why it isn't sent."""
    if senders.emailer is None:
        return "no email provider configured"
    withheld = None
    if delivery.repo_id is not None:
        allowed = await _may_read(db, delivery, senders)
        if allowed is False:
            return "can't read the repository"
        if allowed is None:
            org = await db.get(Org, delivery.org_id)
            withheld = Withheld(org.name, org.slug) if org else Withheld("your organization", "")
    text = email_text(delivery, senders.origin, withheld=withheld)
    subject = delivery.subject if withheld is None else WITHHELD[delivery.kind]
    # ASCII, whatever the address: it goes in a header.
    key = hashlib.sha256(f"{delivery.key}:{delivery.recipient}".encode()).hexdigest()
    await senders.emailer.send(Email(delivery.recipient, subject, text), key=key)
    return None


async def _slack(db: AsyncSession, delivery: NotificationDelivery, senders: Senders) -> str | None:
    """Post to the org's Slack (None), or why it isn't posted."""
    slack = await db.scalar(
        select(SlackIntegration).where(SlackIntegration.org_id == delivery.org_id)
    )
    if slack is None or delivery.kind.value not in (slack.kinds or []):
        return "the org's Slack doesn't post this"
    url = senders.cipher.decrypt_text(slack.webhook_url_enc, context=slack_context(delivery.org_id))
    await senders.slack.post(url, slack_text(delivery, senders.origin))
    return None


async def _may_read(
    db: AsyncSession, delivery: NotificationDelivery, senders: Senders
) -> bool | None:
    """Whether the email's person may read its repository (None: it can't be told)."""
    repo = await db.get(Repo, delivery.repo_id)
    user = await db.get(User, delivery.user_id) if delivery.user_id else None
    if repo is None or repo.deleted_at is not None or user is None:
        return False
    if open_to_all(repo):
        return True
    if senders.can_read is None:
        return None
    return await senders.can_read(user, repo)


def _tried(
    delivery: NotificationDelivery,
    why: str,
    *,
    retry: bool,
    now: datetime,
    senders: Senders,
    done: Delivered,
) -> None:
    """A try that didn't send it: again later (``retry``, while tries are left), or failed."""
    delivery.attempts += 1
    delivery.error = why[:500]
    if retry and delivery.attempts < senders.max_attempts:
        delivery.next_attempt_at = now + FIRST_WAIT * 2 ** (delivery.attempts - 1)
        done.retrying += 1
        return
    delivery.state = DeliveryState.FAILED
    done.failed += 1
    log.warning("notification not delivered", channel=delivery.channel.value,
                key=delivery.key, error=delivery.error)  # fmt: skip


def _end(delivery: NotificationDelivery, state: DeliveryState, why: str, done: Delivered) -> None:
    delivery.state = state
    delivery.error = why
    if state is DeliveryState.SKIPPED:
        done.skipped += 1
    else:
        done.failed += 1
