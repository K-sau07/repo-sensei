"""The invitation email: its words, sent through the notifications' emailer, and whether it was
sent (no provider, or Resend refusing, isn't)."""

from __future__ import annotations

from sensei_core.mail import EmailMailer, InvitationEmail
from sensei_core.notifications.senders import (
    ConsoleEmailer,
    Email,
    Emailer,
    MemoryEmailer,
    SendError,
)

INVITE = InvitationEmail(to="friend@example.com", org_name="Acme", inviter="octo",
                         role="admin", link="https://app.example.com/invite/tok")  # fmt: skip


async def test_an_invitation_is_emailed_with_its_link() -> None:
    emails = MemoryEmailer()
    assert await EmailMailer(lambda: emails).send_invitation(INVITE, key="inv-1")
    [email] = emails.sent
    assert email.to == "friend@example.com"
    assert email.subject == "octo invited you to Acme on repo-sensei"
    assert "join Acme as an admin" in email.text
    assert "Accept it: https://app.example.com/invite/tok" in email.text
    # The same invitation sent again is the same email to Resend (its key is the invitation's).
    await EmailMailer(lambda: emails).send_invitation(INVITE, key="inv-1")
    assert emails.keys[0] == emails.keys[1]
    assert emails.keys[0].isascii()


async def test_one_that_cant_be_sent_says_so() -> None:
    assert not await EmailMailer(lambda: None).send_invitation(INVITE, key="inv-1")
    # Development's emailer logs it; nobody got it.
    assert not await EmailMailer(ConsoleEmailer).send_invitation(INVITE, key="inv-1")
    refusing: Emailer = MemoryEmailer(fail=SendError("422 invalid address", retry=False))
    assert not await EmailMailer(lambda: refusing).send_invitation(INVITE, key="inv-1")


async def test_any_other_failure_is_not_sent_either() -> None:
    class Closed:
        async def send(self, email: Email, *, key: str) -> None:
            raise RuntimeError("Cannot send a request, as the client has been closed.")

    assert not await EmailMailer(Closed).send_invitation(INVITE, key="inv-1")
