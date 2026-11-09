"""Invitation email, sent with the notifications' emailer (Resend; development logs it). Sent
only once the invitation is committed, so the link in it always works. One that wasn't sent (no
provider, only logged in development, Resend refusing or not answering) says so, and the inviter
shares the link."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from sensei_core.logging import get_logger
from sensei_core.notifications.senders import ConsoleEmailer, Email, Emailer, SendError

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InvitationEmail:
    to: str
    org_name: str
    inviter: str
    role: str
    link: str


class Mailer(Protocol):
    async def send_invitation(self, message: InvitationEmail, *, key: str) -> bool:
        """Whether it was sent (``key``: the same invitation isn't emailed twice)."""
        ...


def invitation_email(message: InvitationEmail) -> Email:
    article = "an" if message.role[:1] in "aeiou" else "a"
    subject = f"{message.inviter} invited you to {message.org_name} on repo-sensei"
    text = "\n\n".join([
        f"{message.inviter} invited you to join {message.org_name} as {article} {message.role}.",
        f"Accept it: {message.link}",
        "The link works once, for 7 days. Sign in with this email address or the GitHub "
        "account it was meant for.",
        "-- repo-sensei. Weren't expecting this? You can ignore it.",
    ])  # fmt: skip
    return Email(message.to, subject, text)


class EmailMailer:
    """Sends through the emailer of the moment (``emailer``: None when there's none)."""

    def __init__(self, emailer: Callable[[], Emailer | None]) -> None:
        self._emailer = emailer

    async def send_invitation(self, message: InvitationEmail, *, key: str) -> bool:
        emailer = self._emailer()
        if emailer is None:
            return False
        # ASCII, whatever the address: it goes in a header.
        idempotency = hashlib.sha256(f"invitation:{key}".encode()).hexdigest()
        try:
            await emailer.send(invitation_email(message), key=idempotency)
        except SendError as exc:
            log.warning("invitation email not sent", error=str(exc), retry=exc.retry)
            return False
        # Development's emailer only logs it: nobody got it, so the inviter shares the link.
        return not isinstance(emailer, ConsoleEmailer)


@dataclass
class MemoryMailer:
    """Test double that records messages (``sends``: whether it says they were sent)."""

    sent: list[InvitationEmail] = field(default_factory=list[InvitationEmail])
    sends: bool = True

    async def send_invitation(self, message: InvitationEmail, *, key: str) -> bool:
        self.sent.append(message)
        return self.sends
