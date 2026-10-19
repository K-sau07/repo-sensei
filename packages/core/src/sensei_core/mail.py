"""Outgoing email. Development prints messages to the log; a real provider is plugged in later."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sensei_core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InvitationEmail:
    to: str
    org_name: str
    inviter: str
    role: str
    link: str


class Mailer(Protocol):
    async def send_invitation(self, message: InvitationEmail) -> None: ...


class ConsoleMailer:
    """Development only: logs the message (including the invitation link) instead of sending."""

    async def send_invitation(self, message: InvitationEmail) -> None:
        log.info(
            "invitation email (dev console)",
            to=message.to,
            org=message.org_name,
            inviter=message.inviter,
            role=message.role,
            link=message.link,
        )


@dataclass
class MemoryMailer:
    """Test double that records messages."""

    sent: list[InvitationEmail] = field(default_factory=list[InvitationEmail])

    async def send_invitation(self, message: InvitationEmail) -> None:
        self.sent.append(message)
