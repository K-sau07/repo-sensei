"""Notifications (8.10): what people are told, where.

- a **notification** is one person's inbox item (an approval waiting, a pull request opened,
  a fix that failed, an index done or failed, the org's spend near its cap): its words, where
  it links, whether it was read. A key makes it once per person per event (``fix:<id>:pr``);
- a **delivery** sends one beyond the inbox: an email to the person, or a post to the org's
  Slack channel; a worker sends what's pending (and retries what failed, a few times);
- **preferences** are a person's (in every org), per kind: in the inbox, by email (Slack is
  the org's);
- the org's **Slack** is an incoming webhook (its URL kept encrypted) and the kinds it posts.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin
from sensei_core.db.tenancy import OrgScopedMixin


class NotificationKind(enum.StrEnum):
    APPROVAL_NEEDED = "approval_needed"
    PR_OPENED = "pr_opened"
    FIX_FAILED = "fix_failed"
    INDEX_DONE = "index_done"
    INDEX_FAILED = "index_failed"
    COST_CAP = "cost_cap"


class DeliveryChannel(enum.StrEnum):
    EMAIL = "email"
    SLACK = "slack"


class DeliveryState(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"  # gave up after its tries
    SKIPPED = "skipped"  # nothing to send it with (no provider configured, no address)


def _values(kind: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in kind]


def _enum(kind: type[enum.StrEnum], name: str) -> Enum:
    return Enum(kind, name=name, values_callable=_values)


class Notification(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("uq_notifications_user_key", "user_id", "key", unique=True),
        Index("ix_notifications_inbox", "org_id", "user_id", "created_at"),
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The repository it's about: shown only to those who can read it there (as the repo is).
    repo_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[NotificationKind] = mapped_column(_enum(NotificationKind, "notification_kind"))
    key: Mapped[str] = mapped_column(String(200))  # the event's: once per person
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    link: Mapped[str | None] = mapped_column(String(500))  # a path in the app
    # What the inbox shows beside the words: an approval's risk ({"risk": "high"}).
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NotificationDelivery(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        # Once per channel, recipient and event (the org's Slack hears of an event once).
        Index("uq_notification_deliveries_once", "channel", "recipient", "key", unique=True),
        Index("ix_notification_deliveries_pending", "state", "created_at"),
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
    )

    channel: Mapped[DeliveryChannel] = mapped_column(_enum(DeliveryChannel, "delivery_channel"))
    recipient: Mapped[str] = mapped_column(String(320))  # an email address, or the org's Slack
    # An email's person: one about a repository goes only if they can still read it.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    repo_id: Mapped[uuid.UUID | None] = mapped_column()
    key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[NotificationKind] = mapped_column(_enum(NotificationKind, "notification_kind"))
    subject: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    state: Mapped[DeliveryState] = mapped_column(
        _enum(DeliveryState, "delivery_state"), default=DeliveryState.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # a retry's
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationPreferences(IdMixin, TimestampMixin, Base):
    """A person's, in every org they're in (as their sessions are: not an org's)."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    # kind -> {"in_app": bool, "email": bool}; a kind left out takes the defaults.
    channels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class SlackIntegration(IdMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "slack_integrations"
    __table_args__ = (Index("uq_slack_integrations_org", "org_id", unique=True),)

    webhook_url_enc: Mapped[bytes] = mapped_column(LargeBinary)
    channel: Mapped[str] = mapped_column(String(100), default="", server_default="")
    kinds: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
