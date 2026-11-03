"""Telling people (8.10): an event becomes an inbox item for each person it is for, an email to
those who want one, and a post to the org's Slack when the org has it on for that kind.

Everything is written in the caller's transaction (it commits with the event, or not at all),
once per person and event (a key: ``fix:<id>:pr``; a second call does nothing). Sending
happens later, in a worker (:mod:`.deliver`), from the deliveries written here; the inbox's
live stream is told after the commit (:func:`publish`).

Who is told:

- an approval waiting: whoever asked for the fix (or change) and the org's admins;
- a pull request opened, a fix that failed (or was blocked, or couldn't be reproduced): whoever
  asked for it (the admins, for one Sentry started);
- an index done (a repository's first) or failed: the org's admins;
- the org's spend near its cap: the org's admins.

What they want: in the inbox, everything; by email, all but an index done; a person changes
either per kind.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import orjson
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.logging import get_logger
from sensei_core.models import (
    DeliveryChannel,
    DeliveryState,
    Membership,
    Notification,
    NotificationDelivery,
    NotificationKind,
    NotificationPreferences,
    Org,
    Repo,
    Role,
    SlackIntegration,
    User,
)

log = get_logger(__name__)

KIND_LABELS: dict[NotificationKind, str] = {
    NotificationKind.APPROVAL_NEEDED: "Approvals needed",
    NotificationKind.PR_OPENED: "Pull requests opened",
    NotificationKind.FIX_FAILED: "Fixes and changes that stopped",
    NotificationKind.INDEX_DONE: "Indexing done",
    NotificationKind.INDEX_FAILED: "Indexing failed",
    NotificationKind.COST_CAP: "Spend near the cap",
}
EMAIL_BY_DEFAULT = frozenset(set(NotificationKind) - {NotificationKind.INDEX_DONE})
SLACK_RECIPIENT = "slack"
STREAM = "inbox:{org}:{user}"
STREAM_MAXLEN = 200


@dataclass(frozen=True, slots=True)
class Event:
    """Something to tell people about."""

    org_id: uuid.UUID
    kind: NotificationKind
    key: str  # the event's own, the same each time it is told (once per person)
    title: str
    body: str = ""
    link: str | None = None  # a path in the app (/acme/api/fixes/<id>)
    meta: Mapping[str, Any] | None = None  # shown beside it in the inbox ({"risk": "high"})


@dataclass(frozen=True, slots=True)
class Told:
    """Who an event's inbox items went to (for their live streams, after the commit)."""

    users: list[uuid.UUID]
    event: Event


@dataclass(frozen=True, slots=True)
class RepoPlace:
    """Where a repository is in the app, and what people call it."""

    full_name: str
    path: str  # /<org>/<repo>


async def repo_place(db: AsyncSession, org_id: uuid.UUID, repo_id: uuid.UUID) -> RepoPlace | None:
    found = (
        await db.execute(
            select(Org.slug, Repo.slug, Repo.full_name)
            .join(Repo, Repo.org_id == Org.id)
            .where(Org.id == org_id, Repo.id == repo_id)
        )
    ).first()
    if found is None:
        return None
    org, repo, full_name = found
    return RepoPlace(full_name, f"/{quote(org, safe='')}/{quote(repo, safe='')}")


def wants(channels: Mapping[str, Any], kind: NotificationKind, channel: str) -> bool:
    """Whether a person's preferences (``channels``: kind -> {"in_app", "email"}) say yes."""
    found = channels.get(kind.value)
    if isinstance(found, Mapping):
        said = cast("Mapping[str, object]", found).get(channel)
        if isinstance(said, bool):
            return said
    return channel == "in_app" or (channel == "email" and kind in EMAIL_BY_DEFAULT)


async def admins(db: AsyncSession, org_id: uuid.UUID) -> list[uuid.UUID]:
    found = await db.scalars(
        select(Membership.user_id).where(
            Membership.org_id == org_id, Membership.role.in_((Role.ADMIN, Role.OWNER))
        )
    )
    return list(found)


async def members_among(
    db: AsyncSession, org_id: uuid.UUID, users: Iterable[uuid.UUID]
) -> list[uuid.UUID]:
    """Those of ``users`` still in the org (someone who left is told nothing)."""
    wanted = set(users)
    if not wanted:
        return []
    found = await db.scalars(
        select(Membership.user_id).where(
            Membership.org_id == org_id, Membership.user_id.in_(wanted)
        )
    )
    return list(found)


async def notify(db: AsyncSession, event: Event, users: Iterable[uuid.UUID]) -> Told:
    """Tell ``users`` (the org's members among them) of ``event``; the people newly told (their
    inbox streams are told after the commit: :func:`publish`)."""
    people = await members_among(db, event.org_id, users)
    told: list[uuid.UUID] = []
    if people:
        prefs = {
            p.user_id: p.channels
            for p in await db.scalars(
                select(NotificationPreferences).where(NotificationPreferences.user_id.in_(people))
            )
        }
        emails = dict(
            (await db.execute(select(User.id, User.email).where(User.id.in_(people))))
            .tuples()
            .all()
        )
        for user_id in people:
            channels = prefs.get(user_id, {})
            if wants(channels, event.kind, "in_app") and await _inbox(db, event, user_id):
                told.append(user_id)
            email = emails.get(user_id)
            if email and wants(channels, event.kind, "email"):
                await _delivery(db, event, DeliveryChannel.EMAIL, email)
    slack = await db.scalar(select(SlackIntegration).where(SlackIntegration.org_id == event.org_id))
    if slack is not None and event.kind.value in (slack.kinds or []):
        await _delivery(db, event, DeliveryChannel.SLACK, SLACK_RECIPIENT)
    return Told(told, event)


async def _inbox(db: AsyncSession, event: Event, user_id: uuid.UUID) -> bool:
    found = await db.execute(
        pg_insert(Notification)
        .values(
            org_id=event.org_id,
            user_id=user_id,
            kind=event.kind,
            key=event.key,
            title=event.title[:300],
            body=event.body,
            link=event.link,
            meta=dict(event.meta or {}),
        )
        .on_conflict_do_nothing(index_elements=["user_id", "key"])
        .returning(Notification.id)
    )
    return found.scalar() is not None


async def _delivery(db: AsyncSession, event: Event, channel: DeliveryChannel, to: str) -> None:
    await db.execute(
        pg_insert(NotificationDelivery)
        .values(
            org_id=event.org_id,
            channel=channel,
            recipient=to[:320],
            key=event.key,
            kind=event.kind,
            subject=event.title[:300],
            text=event.body,
            link=event.link,
            state=DeliveryState.PENDING,
        )
        .on_conflict_do_nothing(index_elements=["channel", "recipient", "key"])
    )


def inbox_stream(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return STREAM.format(org=org_id, user=user_id)


async def publish(redis: Redis, told: Iterable[Told]) -> None:
    """Tell each person's open inbox (its live stream) that something arrived. Call after the
    commit; a lost message only delays the inbox until it is read again."""
    for each in told:
        event = each.event
        data = orjson.dumps({"type": "inbox.new", "kind": event.kind.value, "key": event.key,
                             "title": event.title, "link": event.link}).decode()  # fmt: skip
        try:
            for user_id in each.users:
                await redis.xadd(inbox_stream(event.org_id, user_id), {"data": data},  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
                                 maxlen=STREAM_MAXLEN, approximate=True)  # fmt: skip
        except RedisError:
            log.warning("inbox stream not told", key=event.key)
