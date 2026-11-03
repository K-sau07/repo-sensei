"""Notifications (8.10): a person's inbox in an org, what they want to be told and how, and the
org's Slack.

- ``GET /orgs/{org}/inbox``: their inbox items, newest first (``unread``: only those), with how
  many are unread; ``POST /orgs/{org}/inbox/read``: some (``ids``) or all of them read;
  ``GET /orgs/{org}/inbox/events``: new items as they arrive (server-sent events, ``inbox.new``;
  a browser session's, until it ends or they leave the org);
- ``GET/PATCH /me/notifications``: their preferences, per kind: in the inbox, by email (the
  same in every org);
- ``PUT/GET/DELETE /orgs/{org}/integrations/slack`` (admins): the org's Slack incoming webhook
  (kept encrypted, never shown again but for its end) and the kinds it posts.

An item about a repository (a fix's, an index run's) is shown only to those who can read that
repository on GitHub, as the repository's own pages are (``repo_id``): someone who lost access
no longer sees it, in the list, the unread count or the live stream.

API keys can't use any of it (a person's, not a script's).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sse_starlette import EventSourceResponse

from sensei_api import stream_access, streams
from sensei_api.auth import CurrentUser, RequiredSession
from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoAccessDep
from sensei_api.routers.auth import GitHubOAuthDep
from sensei_core.audit import AuditAction, audit
from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.github.client import GitHubError
from sensei_core.github.oauth import OAuthError
from sensei_core.models import (
    FixRisk,
    Notification,
    NotificationKind,
    NotificationPreferences,
    Repo,
    Role,
    SlackIntegration,
)
from sensei_core.notifications.deliver import slack_context
from sensei_core.notifications.senders import valid_slack_webhook
from sensei_core.notifications.service import KIND_LABELS, inbox_stream, wants
from sensei_core.orgs.service import OrgAccess, require_role
from sensei_core.repos.access import RepoAccess, open_to_all
from sensei_core.repos.service import GitHubUnavailableError
from sensei_core.security.crypto import TokenCipher

router = APIRouter(tags=["notifications"])

MAX_PAGE = 100
MEMBERSHIP_RECHECK_SECONDS = 60.0


class InboxItemOut(BaseModel):
    id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str
    link: str | None  # a path in the app
    risk: FixRisk | None  # an approval's
    read: bool
    created_at: datetime


class InboxPage(BaseModel):
    items: list[InboxItemOut]
    unread: int
    next_cursor: uuid.UUID | None  # ``before`` for the next (older) page


class InboxReadIn(BaseModel):
    ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_PAGE)  # None: all
    kinds: list[NotificationKind] | None = Field(default=None, max_length=len(NotificationKind))


class InboxCount(BaseModel):
    unread: int


async def _shown(db: DbSession, access: OrgAccess, readers: RepoAccess) -> ColumnElement[bool]:
    """Which of the person's items they may see now: none about a repository they can't read
    (or one since removed). GitHub is asked about each repository their items name, once per
    five minutes (its answers are cached); a lapsed GitHub sign-in shows only public ones'."""
    named = list(
        await db.scalars(
            select(Notification.repo_id)
            .where(
                Notification.org_id == access.org.id,
                Notification.user_id == access.user.id,
                Notification.repo_id.is_not(None),
            )
            .distinct()
        )
    )
    if not named:
        return true()
    repos = list(
        await db.scalars(
            select(Repo).where(
                Repo.org_id == access.org.id, Repo.id.in_(named), Repo.deleted_at.is_(None)
            )
        )
    )
    try:
        readable = await readers.readable(access.user, repos)
    except OAuthError:
        readable = [repo for repo in repos if open_to_all(repo)]
    except GitHubError as exc:
        raise GitHubUnavailableError("GitHub is not responding.") from exc
    allowed = {repo.id for repo in readable}
    hidden = [repo_id for repo_id in named if repo_id not in allowed]
    if not hidden:
        return true()
    return or_(Notification.repo_id.is_(None), Notification.repo_id.not_in(hidden))


async def _unread(
    db: DbSession, org_id: uuid.UUID, user_id: uuid.UUID, shown: ColumnElement[bool]
) -> int:
    found = await db.scalar(
        select(func.count()).where(
            Notification.org_id == org_id,
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            shown,
        )
    )
    return int(found or 0)


def _risk(meta: dict[str, Any] | None) -> FixRisk | None:
    found = (meta or {}).get("risk")
    return FixRisk(found) if found in {risk.value for risk in FixRisk} else None


@router.get("/orgs/{org}/inbox", response_model=InboxPage)
async def get_inbox(
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
    *,
    unread: bool = False,
    kinds: Annotated[list[NotificationKind] | None, Query(max_length=len(NotificationKind))] = None,
    before: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
) -> InboxPage:
    """The person's items, newest first (``unread``: only those; ``kinds``: only those kinds),
    with how many are unread in all."""
    org_id, user_id = access.org.id, access.user.id
    shown = await _shown(db, access, readers)
    query = select(Notification).where(
        Notification.org_id == org_id, Notification.user_id == user_id, shown
    )
    if kinds:
        query = query.where(Notification.kind.in_(kinds))
    if unread:
        query = query.where(Notification.read_at.is_(None))
    if before is not None:
        query = query.where(Notification.id < before)
    rows = list(await db.scalars(query.order_by(Notification.id.desc()).limit(limit)))
    items = [
        InboxItemOut(
            id=row.id,
            kind=row.kind,
            title=row.title,
            body=row.body,
            link=row.link,
            risk=_risk(row.meta),
            read=row.read_at is not None,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return InboxPage(
        items=items,
        unread=await _unread(db, org_id, user_id, shown),
        next_cursor=rows[-1].id if len(rows) == limit else None,
    )


@router.post("/orgs/{org}/inbox/read", response_model=InboxCount)
async def read_inbox(
    body: InboxReadIn, access: OrgAccessDep, db: DbSession, readers: RepoAccessDep
) -> InboxCount:
    """Mark ``ids`` read (others' and unknown ones are passed over), or every item (``kinds``:
    every item of those kinds)."""
    org_id, user_id = access.org.id, access.user.id
    shown = await _shown(db, access, readers)
    marking = (
        update(Notification)
        .where(
            Notification.org_id == org_id,
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    if body.ids is not None:
        marking = marking.where(Notification.id.in_(body.ids))
    if body.kinds:
        marking = marking.where(Notification.kind.in_(body.kinds))
    await db.execute(marking.where(shown))
    return InboxCount(unread=await _unread(db, org_id, user_id, shown))


@router.get(
    "/orgs/{org}/inbox/events",
    response_class=EventSourceResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def inbox_events(
    request: Request,
    access: OrgAccessDep,
    active: RequiredSession,
    redis: RedisClient,
    oauth: GitHubOAuthDep,
) -> EventSourceResponse:
    """New inbox items (``inbox.new``: its kind, title and link), from now (or after
    ``Last-Event-ID``); one about a repository only while the user can read it. Ends when the
    session does or the user leaves the org (checked every minute)."""
    org_id, user_id, session_id = access.org.id, access.user.id, active.session.id
    key = inbox_stream(org_id, user_id)
    resuming = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    cursor = streams.last_event_id(request) if resuming else await streams.latest_id(redis, key)
    readable = stream_access.readable_repos(request, oauth, org_id, user_id)
    allowed = True

    async def still() -> bool:
        nonlocal allowed
        allowed = await streams.still_member(request, org_id, user_id, session_id)
        return allowed

    async def keep(payload: dict[str, Any]) -> bool:
        if not allowed:
            return False
        repo_id = payload.get("repo_id")
        return repo_id is None or await readable(str(repo_id))

    return streams.response(
        streams.follow(
            request,
            redis,
            key,
            cursor,
            keep=keep,
            still=still,
            recheck_seconds=MEMBERSHIP_RECHECK_SECONDS,
        )
    )


# ---------------------------------------------------------------- preferences


class KindPreference(BaseModel):
    kind: NotificationKind
    label: str
    in_app: bool
    email: bool


class PreferencesOut(BaseModel):
    kinds: list[KindPreference]
    email: str | None  # where email goes (None: nowhere, the account has no address)


class ChannelsPatch(BaseModel):
    in_app: bool | None = None
    email: bool | None = None


class PreferencesPatch(BaseModel):
    kinds: dict[NotificationKind, ChannelsPatch] = Field(max_length=len(NotificationKind))


def _preferences(channels: dict[str, Any], email: str | None) -> PreferencesOut:
    return PreferencesOut(
        kinds=[
            KindPreference(
                kind=kind,
                label=KIND_LABELS[kind],
                in_app=wants(channels, kind, "in_app"),
                email=wants(channels, kind, "email"),
            )
            for kind in NotificationKind
        ],
        email=email,
    )


@router.get("/me/notifications", response_model=PreferencesOut)
async def get_preferences(user: CurrentUser, db: DbSession) -> PreferencesOut:
    found = await db.scalar(
        select(NotificationPreferences).where(NotificationPreferences.user_id == user.id)
    )
    return _preferences(found.channels if found else {}, user.email)


@router.patch("/me/notifications", response_model=PreferencesOut)
async def update_preferences(
    body: PreferencesPatch, user: CurrentUser, db: DbSession
) -> PreferencesOut:
    # Made first if it isn't there (two first changes at once: one makes it, both then lock it).
    await db.execute(
        pg_insert(NotificationPreferences)
        .values(user_id=user.id, channels={})
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    found = await db.scalar(
        select(NotificationPreferences)
        .where(NotificationPreferences.user_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if found is None:  # pragma: no cover - made just above
        raise NotFoundError("Preferences not found.", code="preferences_not_found")
    channels: dict[str, Any] = {
        kind.value: {
            "in_app": wants(found.channels, kind, "in_app"),
            "email": wants(found.channels, kind, "email"),
        }
        for kind in NotificationKind
    }
    for kind, patch in body.kinds.items():
        for channel in ("in_app", "email"):
            value = getattr(patch, channel)
            if value is not None:
                channels[kind.value][channel] = value
    found.channels = channels
    await db.flush()
    return _preferences(channels, user.email)


# ---------------------------------------------------------------- the org's Slack


class SlackIn(BaseModel):
    # Required to connect; left out of a change, the webhook stays as it was.
    webhook_url: str | None = Field(default=None, max_length=500)
    channel: str = Field(default="", max_length=100)  # what it's called, to show
    kinds: list[NotificationKind] = Field(
        default_factory=list[NotificationKind], max_length=len(NotificationKind)
    )


class SlackOut(BaseModel):
    channel: str
    kinds: list[NotificationKind]
    webhook_hint: str  # the URL's last characters (it is never shown again)


def _cipher(request: Request) -> TokenCipher:
    cipher: TokenCipher = request.app.state.token_cipher
    return cipher


def _slack_out(found: SlackIntegration, hint: str) -> SlackOut:
    known = {kind.value for kind in NotificationKind}
    kinds = [NotificationKind(k) for k in found.kinds if k in known]
    return SlackOut(channel=found.channel, kinds=kinds, webhook_hint=hint)


def _hint(url: str) -> str:
    return f"…{url[-4:]}"


@router.put("/orgs/{org}/integrations/slack", response_model=SlackOut)
async def put_slack(
    request: Request, body: SlackIn, access: OrgAccessDep, db: DbSession
) -> SlackOut:
    """Connect the org's Slack (or change what it posts). Admins."""
    require_role(access, Role.ADMIN)
    org_id = access.org.id
    cipher = _cipher(request)
    url = body.webhook_url.strip() if body.webhook_url is not None else None
    if url is not None and not valid_slack_webhook(url):
        raise InvalidError("That isn't a Slack incoming webhook (https://hooks.slack.com/services/…).",
                           code="slack_webhook")  # fmt: skip
    if url is not None:
        # Made if it isn't there (two first connections at once: one makes it, both change it).
        await db.execute(
            pg_insert(SlackIntegration)
            .values(org_id=org_id, created_by=access.user.id,
                    webhook_url_enc=cipher.encrypt_text(url, context=slack_context(org_id)))
            .on_conflict_do_nothing(index_elements=["org_id"])
        )  # fmt: skip
    found = await db.scalar(
        select(SlackIntegration)
        .where(SlackIntegration.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if found is None:
        raise InvalidError("Connecting Slack needs its incoming webhook's URL.",
                           code="slack_webhook")  # fmt: skip
    if url is not None:
        found.webhook_url_enc = cipher.encrypt_text(url, context=slack_context(org_id))
    found.channel = body.channel.strip()
    found.kinds = sorted({kind.value for kind in body.kinds})
    await db.flush()
    await audit(db, AuditAction.SLACK_CONNECT, actor_user_id=access.user.id, org_id=org_id,
                meta={"channel": found.channel, "kinds": found.kinds,
                      "webhook_changed": url is not None})  # fmt: skip
    current = url or cipher.decrypt_text(found.webhook_url_enc, context=slack_context(org_id))
    return _slack_out(found, _hint(current))


@router.get("/orgs/{org}/integrations/slack", response_model=SlackOut)
async def get_slack(request: Request, access: OrgAccessDep, db: DbSession) -> SlackOut:
    require_role(access, Role.ADMIN)
    found = await db.scalar(
        select(SlackIntegration).where(SlackIntegration.org_id == access.org.id)
    )
    if found is None:
        raise NotFoundError("Slack isn't connected.", code="slack_not_connected")
    url = _cipher(request).decrypt_text(found.webhook_url_enc, context=slack_context(access.org.id))
    return _slack_out(found, _hint(url))


@router.delete("/orgs/{org}/integrations/slack", status_code=status.HTTP_204_NO_CONTENT)
async def delete_slack(access: OrgAccessDep, db: DbSession) -> None:
    require_role(access, Role.ADMIN)
    found = await db.scalar(
        select(SlackIntegration).where(SlackIntegration.org_id == access.org.id)
    )
    if found is not None:
        await db.delete(found)
        await audit(db, AuditAction.SLACK_DISCONNECT, actor_user_id=access.user.id,
                    org_id=access.org.id)  # fmt: skip
