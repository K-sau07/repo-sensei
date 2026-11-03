"""Notifications (8.10): an event told once per person (inbox, email as they prefer, the org's
Slack for the kinds it posts), only to the org's members; deliveries sent, retried, given up
or skipped; Slack webhooks only Slack's; and an index run's end told when it is news."""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import orjson
import pytest
import respx
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import NotifySettings, Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    DeliveryChannel,
    DeliveryState,
    IndexJob,
    JobKind,
    JobStatus,
    Membership,
    Notification,
    NotificationDelivery,
    NotificationKind,
    NotificationPreferences,
    Repo,
    RepoSource,
    Role,
    SlackIntegration,
    User,
)
from sensei_core.notifications.deliver import (
    Senders,
    deliver_pending,
    email_text,
    slack_context,
    slack_text,
)
from sensei_core.notifications.indexing import index_notice
from sensei_core.notifications.senders import (
    HttpSlackPoster,
    MemoryEmailer,
    MemorySlackPoster,
    ResendEmailer,
    SendError,
    emailer,
    valid_slack_webhook,
)
from sensei_core.notifications.service import Event, inbox_stream, notify, publish, wants
from sensei_core.orgs.service import OrgService
from sensei_core.security.crypto import TokenCipher
from sensei_testing import make_user

pytestmark = pytest.mark.integration

ORIGIN = "https://app.example.com"
HOOK = "https://hooks.slack.com/services/T0001/B0002/abcDEF123"


def _cipher() -> TokenCipher:
    return TokenCipher(base64.b64decode(base64.b64encode(os.urandom(32))))


async def _org(db: AsyncSession) -> tuple[uuid.UUID, User, Repo]:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), ORIGIN).create(
        db, owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}"
    )
    await set_org_context(db, access.org.id)
    repo = Repo(
        org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=7, owner="o",
        name="items", full_name="o/items", slug="items", default_branch="main",
        visibility="public",
    )  # fmt: skip
    db.add(repo)
    await db.flush()
    return access.org.id, owner, repo


async def _member(db: AsyncSession, org_id: uuid.UUID, role: Role, **user: Any) -> User:
    person = await make_user(db, **user)
    db.add(Membership(org_id=org_id, user_id=person.id, role=role))
    await db.flush()
    return person


def _event(org_id: uuid.UUID, kind: NotificationKind = NotificationKind.FIX_FAILED,
           key: str = "fix:1:failed") -> Event:  # fmt: skip
    return Event(org_id, kind, key, "Fix failed: KeyError", "In o/items.", "/acme/items/fixes/1")


async def _inbox(db: AsyncSession, org_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    rows = await db.execute(
        select(Notification.user_id, Notification.key).where(Notification.org_id == org_id)
    )
    return sorted(rows.tuples().all(), key=str)


async def _deliveries(db: AsyncSession, org_id: uuid.UUID) -> list[NotificationDelivery]:
    found = await db.scalars(
        select(NotificationDelivery)
        .where(NotificationDelivery.org_id == org_id)
        .order_by(NotificationDelivery.channel, NotificationDelivery.recipient)
    )
    return list(found)


def _same(db: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def session() -> AsyncGenerator[AsyncSession]:
        yield db

    return session


# ---------------------------------------------------------------- telling people


async def test_an_event_is_told_once_to_each_member_in_the_inbox_and_by_email(
    db_session: AsyncSession,
) -> None:
    org_id, owner, _ = await _org(db_session)
    member = await _member(db_session, org_id, Role.MEMBER)
    no_email = await _member(db_session, org_id, Role.MEMBER, email=None)
    outsider = await make_user(db_session)  # not in the org: told nothing
    event = _event(org_id)

    told = await notify(db_session, event, [owner.id, member.id, no_email.id, outsider.id])

    assert sorted(told.users, key=str) == sorted([owner.id, member.id, no_email.id], key=str)
    assert await _inbox(db_session, org_id) == sorted(
        [(owner.id, event.key), (member.id, event.key), (no_email.id, event.key)], key=str
    )
    emails = await _deliveries(db_session, org_id)
    assert [(d.channel, d.recipient, d.state) for d in emails] == [
        (DeliveryChannel.EMAIL, owner.email, DeliveryState.PENDING),
        (DeliveryChannel.EMAIL, member.email, DeliveryState.PENDING),
    ]  # the one without an address gets the inbox item only
    # Told again (a retried stage, a message delivered twice): nothing new, nobody re-told.
    again = await notify(db_session, event, [owner.id, member.id])
    assert again.users == []
    assert len(await _inbox(db_session, org_id)) == 3
    assert len(await _deliveries(db_session, org_id)) == 2


async def test_preferences_choose_the_inbox_and_email_per_kind(db_session: AsyncSession) -> None:
    org_id, owner, _ = await _org(db_session)
    quiet = await _member(db_session, org_id, Role.MEMBER)
    db_session.add(NotificationPreferences(user_id=quiet.id, channels={
        "fix_failed": {"in_app": False, "email": False},
        "index_done": {"email": True},  # off by default, on for them
    }))  # fmt: skip
    await db_session.flush()

    await notify(db_session, _event(org_id), [owner.id, quiet.id])
    done = _event(org_id, NotificationKind.INDEX_DONE, "repo:1:first-index")
    await notify(db_session, done, [owner.id, quiet.id])

    assert await _inbox(db_session, org_id) == sorted([
        (owner.id, "fix:1:failed"), (owner.id, "repo:1:first-index"),
        (quiet.id, "repo:1:first-index"),
    ], key=str)  # fmt: skip
    sent = [(d.recipient, d.key) for d in await _deliveries(db_session, org_id)]
    assert sorted(sent) == sorted([
        (owner.email, "fix:1:failed"),  # the owner's defaults: no email for an index done
        (quiet.email, "repo:1:first-index"),
    ])  # fmt: skip


def test_defaults_and_bad_preferences() -> None:
    assert wants({}, NotificationKind.APPROVAL_NEEDED, "email")
    assert not wants({}, NotificationKind.INDEX_DONE, "email")
    assert wants({}, NotificationKind.INDEX_DONE, "in_app")
    # A value that isn't a yes or no is the default, not a no.
    assert wants({"pr_opened": {"email": "no"}}, NotificationKind.PR_OPENED, "email")
    assert wants({"pr_opened": ["email"]}, NotificationKind.PR_OPENED, "in_app")


async def test_the_orgs_slack_hears_of_the_kinds_it_posts_once_per_event(
    db_session: AsyncSession,
) -> None:
    org_id, owner, _ = await _org(db_session)
    cipher = _cipher()
    db_session.add(SlackIntegration(
        org_id=org_id, webhook_url_enc=cipher.encrypt_text(HOOK, context=slack_context(org_id)),
        kinds=["fix_failed"],
    ))  # fmt: skip
    await db_session.flush()

    await notify(db_session, _event(org_id), [owner.id])
    await notify(db_session, _event(org_id), [owner.id])
    await notify(db_session, _event(org_id, NotificationKind.PR_OPENED, "fix:1:pr"), [owner.id])
    # Nobody to tell in the inbox: the org's Slack still hears of it.
    await notify(db_session, _event(org_id, key="fix:2:failed"), [])

    slack = [d.key for d in await _deliveries(db_session, org_id)
             if d.channel is DeliveryChannel.SLACK]  # fmt: skip
    assert sorted(slack) == ["fix:1:failed", "fix:2:failed"]


async def test_publish_tells_each_persons_inbox_stream() -> None:
    redis = FakeAsyncRedis()
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    event = _event(org_id)
    from sensei_core.notifications.service import Told  # noqa: PLC0415

    await publish(redis, [Told([user_id], event)])

    entries = await redis.xrange(inbox_stream(org_id, user_id))
    assert len(entries) == 1
    payload = orjson.loads(entries[0][1][b"data"])
    assert payload == {"type": "inbox.new", "kind": "fix_failed", "key": "fix:1:failed",
                       "title": event.title, "link": event.link}  # fmt: skip


# ---------------------------------------------------------------- sending


def _senders(cipher: TokenCipher, emails: MemoryEmailer | None, slack: MemorySlackPoster,
             max_attempts: int = 3) -> Senders:  # fmt: skip
    return Senders(emailer=emails, slack=slack, cipher=cipher, origin=ORIGIN,
                   max_attempts=max_attempts)  # fmt: skip


async def test_pending_emails_and_slack_posts_are_sent_once(db_session: AsyncSession) -> None:
    org_id, owner, _ = await _org(db_session)
    cipher = _cipher()
    db_session.add(SlackIntegration(
        org_id=org_id, webhook_url_enc=cipher.encrypt_text(HOOK, context=slack_context(org_id)),
        kinds=["fix_failed"],
    ))  # fmt: skip
    await db_session.flush()
    await notify(db_session, _event(org_id), [owner.id])
    emails, slack = MemoryEmailer(), MemorySlackPoster()

    done = await deliver_pending(_same(db_session), _senders(cipher, emails, slack))

    assert (done.sent, done.failed, done.retrying, done.skipped) == (2, 0, 0, 0)
    assert [e.to for e in emails.sent] == [owner.email]
    assert emails.sent[0].subject == "Fix failed: KeyError"
    assert f"Open it: {ORIGIN}/acme/items/fixes/1" in emails.sent[0].text
    assert f"{ORIGIN}/settings/notifications" in emails.sent[0].text
    link = f"<{ORIGIN}/acme/items/fixes/1|Open in repo-sensei>"
    assert slack.posted == [(HOOK, f"*Fix failed: KeyError*\nIn o/items.\n{link}")]
    assert {d.state for d in await _deliveries(db_session, org_id)} == {DeliveryState.SENT}
    # Nothing pending: the next round sends nothing.
    again = await deliver_pending(_same(db_session), _senders(cipher, emails, slack))
    assert again.sent == 0
    assert len(emails.sent) == 1


async def test_a_failing_send_is_retried_a_few_times_then_given_up(
    db_session: AsyncSession,
) -> None:
    org_id, owner, _ = await _org(db_session)
    await notify(db_session, _event(org_id), [owner.id])
    cipher = _cipher()
    flaky = MemoryEmailer(fail=SendError("email: HTTP 503", retry=True))
    senders = _senders(cipher, flaky, MemorySlackPoster(), max_attempts=2)

    first = await deliver_pending(_same(db_session), senders)
    [delivery] = await _deliveries(db_session, org_id)
    assert (first.retrying, delivery.state, delivery.attempts) == (1, DeliveryState.PENDING, 1)
    second = await deliver_pending(_same(db_session), senders)
    assert (second.failed, delivery.state, delivery.attempts) == (1, DeliveryState.FAILED, 2)
    assert delivery.error == "email: HTTP 503"


async def test_a_send_that_cant_pass_fails_at_once(db_session: AsyncSession) -> None:
    org_id, owner, _ = await _org(db_session)
    await notify(db_session, _event(org_id), [owner.id])
    refused = MemoryEmailer(fail=SendError("email: HTTP 422", retry=False))

    done = await deliver_pending(
        _same(db_session), _senders(_cipher(), refused, MemorySlackPoster())
    )

    [delivery] = await _deliveries(db_session, org_id)
    assert (done.failed, delivery.state, delivery.attempts) == (1, DeliveryState.FAILED, 1)


async def test_nothing_to_send_with_is_skipped(db_session: AsyncSession) -> None:
    org_id, owner, _ = await _org(db_session)
    cipher = _cipher()
    slack = SlackIntegration(
        org_id=org_id, webhook_url_enc=cipher.encrypt_text(HOOK, context=slack_context(org_id)),
        kinds=["fix_failed"],
    )  # fmt: skip
    db_session.add(slack)
    await db_session.flush()
    await notify(db_session, _event(org_id), [owner.id])
    await db_session.delete(slack)  # removed before it was sent
    await db_session.flush()

    done = await deliver_pending(_same(db_session), _senders(cipher, None, MemorySlackPoster()))

    assert done.skipped == 2
    states = {(d.channel, d.state, d.error) for d in await _deliveries(db_session, org_id)}
    assert states == {
        (DeliveryChannel.EMAIL, DeliveryState.SKIPPED, "no email provider configured"),
        (DeliveryChannel.SLACK, DeliveryState.SKIPPED, "the org's Slack doesn't post this"),
    }


async def test_a_delivery_pending_for_over_a_day_is_skipped(db_session: AsyncSession) -> None:
    org_id, owner, _ = await _org(db_session)
    await notify(db_session, _event(org_id), [owner.id])
    emails = MemoryEmailer()
    later = datetime.now(UTC) + timedelta(days=2)

    done = await deliver_pending(
        _same(db_session), _senders(_cipher(), emails, MemorySlackPoster()), clock=lambda: later
    )

    assert (done.skipped, emails.sent) == (1, [])


async def test_a_webhook_another_orgs_key_sealed_isnt_posted_to(db_session: AsyncSession) -> None:
    org_id, owner, _ = await _org(db_session)
    cipher = _cipher()
    db_session.add(SlackIntegration(
        org_id=org_id, kinds=["fix_failed"],
        webhook_url_enc=cipher.encrypt_text(HOOK, context=slack_context(uuid.uuid4())),
    ))  # fmt: skip
    await db_session.flush()
    await notify(db_session, _event(org_id), [owner.id])
    slack = MemorySlackPoster()

    await deliver_pending(_same(db_session), _senders(cipher, MemoryEmailer(), slack))

    assert slack.posted == []
    failed = [
        d for d in await _deliveries(db_session, org_id) if d.channel is DeliveryChannel.SLACK
    ]
    assert failed[0].state is DeliveryState.FAILED


def test_texts_link_only_into_the_app_and_escape_slack() -> None:
    delivery = NotificationDelivery(subject="Fix <b> & co", text="a < b", link="//evil.example/x",
                                    channel=DeliveryChannel.SLACK)  # fmt: skip
    assert slack_text(delivery, ORIGIN) == "*Fix &lt;b&gt; &amp; co*\na &lt; b"
    assert "evil" not in email_text(delivery, ORIGIN)
    delivery.link = "https://evil.example/x"
    assert "evil" not in slack_text(delivery, ORIGIN)


# ---------------------------------------------------------------- senders


def test_only_slacks_own_webhooks_are_accepted() -> None:
    assert valid_slack_webhook(HOOK)
    for url in (
        "http://hooks.slack.com/services/T0001/B0002/abc",  # not https
        "https://hooks.slack.com.evil.example/services/T0001/B0002/abc",
        "https://evil.example/?https://hooks.slack.com/services/T0001/B0002/abc",
        "https://hooks.slack.com/services/T0001/B0002/abc?x=1",
        "https://hooks.slack.com/services/T0001/B0002/abc#x",
        "https://user@hooks.slack.com/services/T0001/B0002/abc",
        "https://hooks.slack.com/workflows/T0001/abc",
        "https://hooks.slack.com/services/",
    ):
        assert not valid_slack_webhook(url), url


async def test_resend_is_sent_with_its_key_and_an_idempotency_key() -> None:
    settings = Settings(notify=NotifySettings(resend_api_key=SecretStr("re_test")))
    async with httpx.AsyncClient() as http, respx.mock() as mock:
        route = mock.post("https://api.resend.com/emails").respond(200, json={"id": "e1"})
        sender = ResendEmailer(http, settings)
        from sensei_core.notifications.senders import Email  # noqa: PLC0415

        await sender.send(Email("a@example.com", "Hi", "Body"), key="fix:1:pr:a@example.com")
        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer re_test"
        assert request.headers["idempotency-key"] == "fix:1:pr:a@example.com"
        assert orjson.loads(request.content) == {
            "from": settings.notify.email_from, "to": ["a@example.com"], "subject": "Hi",
            "text": "Body",
        }  # fmt: skip
        mock.post("https://api.resend.com/emails").respond(503)
        with pytest.raises(SendError) as busy:
            await sender.send(Email("a@example.com", "Hi", "Body"), key="k")
        assert busy.value.retry
        mock.post("https://api.resend.com/emails").respond(422)
        with pytest.raises(SendError) as wrong:
            await sender.send(Email("a@example.com", "Hi", "Body"), key="k")
        assert not wrong.value.retry


async def test_slack_posts_go_only_to_slack_and_dont_follow_redirects() -> None:
    async with httpx.AsyncClient() as http, respx.mock() as mock:
        poster = HttpSlackPoster(http)
        with pytest.raises(SendError) as refused:
            await poster.post("https://evil.example/hook", "hi")
        assert not refused.value.retry
        mock.post(HOOK).respond(302, headers={"location": "https://evil.example/"})
        with pytest.raises(SendError):
            await poster.post(HOOK, "hi")
        assert len(mock.calls) == 1  # the redirect wasn't followed


def test_without_a_key_development_logs_email_and_elsewhere_skips_it() -> None:
    http = httpx.AsyncClient()
    assert emailer(http, Settings(env="dev")) is not None
    assert emailer(http, Settings(env="test")) is None


# ---------------------------------------------------------------- index runs


async def _job(db: AsyncSession, repo: Repo, status: JobStatus, **extra: Any) -> IndexJob:
    job = IndexJob(org_id=repo.org_id, repo_id=repo.id, kind=JobKind.FULL, status=status,
                   finished_at=datetime.now(UTC), **extra)  # fmt: skip
    db.add(job)
    await db.flush()
    return job


async def test_an_index_run_tells_the_admins_of_a_first_index_and_of_a_new_failure(
    db_session: AsyncSession,
) -> None:
    org_id, owner, repo = await _org(db_session)
    admin = await _member(db_session, org_id, Role.ADMIN)
    member = await _member(db_session, org_id, Role.MEMBER)  # not an admin: told nothing

    first = await index_notice(db_session, await _job(db_session, repo, JobStatus.SUCCEEDED))
    assert first is not None
    assert sorted(first.users, key=str) == sorted([owner.id, admin.id], key=str)
    assert first.event.title == "o/items is indexed"
    assert first.event.link is not None
    assert first.event.link.endswith("/items")
    # A push's run later: routine.
    assert await index_notice(db_session, await _job(db_session, repo, JobStatus.SUCCEEDED)) is None
    failing = await _job(db_session, repo, JobStatus.FAILED,
                         error={"code": "clone", "message": "GitHub said no."})  # fmt: skip
    failed = await index_notice(db_session, failing)
    assert failed is not None
    assert (failed.event.title, failed.event.body) == ("Indexing o/items failed", "GitHub said no.")
    assert failed.event.link is not None
    assert failed.event.link.endswith("/items/index")
    # Failing again: said already. Then a success, then a failure: said again.
    assert await index_notice(db_session, await _job(db_session, repo, JobStatus.FAILED)) is None
    await _job(db_session, repo, JobStatus.SUCCEEDED)
    assert (
        await index_notice(db_session, await _job(db_session, repo, JobStatus.FAILED)) is not None
    )
    assert member.id not in {user for user, _ in await _inbox(db_session, org_id)}
