"""GitHub webhooks: signatures, the delivery inbox and the handlers workers run."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import Settings
from sensei_core.db import make_system_database
from sensei_core.github import webhooks
from sensei_core.github.client import GitHubError
from sensei_core.github.webhooks import (
    HandlerContext,
    handler_for,
    process_delivery,
    record_delivery,
    sign,
    signature_valid,
)
from sensei_core.models import (
    ActorType,
    AuditEvent,
    DeliveryStatus,
    GitHubInstallation,
    Org,
    WebhookDelivery,
)

SECRET = "It's a Secret to Everybody"
INSTALLATION = 31337

# ------------------------------------------------------------------ signatures


# GitHub's example from "Validating webhook deliveries".
EXAMPLE_BODY = b"Hello, World!"
EXAMPLE_HEX = "757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"


def test_signature_matches_githubs_documented_example() -> None:
    assert sign(SECRET, EXAMPLE_BODY) == f"sha256={EXAMPLE_HEX}"
    assert signature_valid(SECRET, EXAMPLE_BODY, f"sha256={EXAMPLE_HEX}")


@pytest.mark.parametrize(
    ("secret", "body", "header"),
    [
        ("wrong secret", EXAMPLE_BODY, f"sha256={EXAMPLE_HEX}"),
        (SECRET, b"Hello, World?", f"sha256={EXAMPLE_HEX}"),  # body changed after signing
        (SECRET, EXAMPLE_BODY, f"sha256={EXAMPLE_HEX[:-1]}0"),
        (SECRET, EXAMPLE_BODY, EXAMPLE_HEX),  # no algorithm prefix
        (SECRET, EXAMPLE_BODY, f"sha1={EXAMPLE_HEX}"),
        (SECRET, EXAMPLE_BODY, ""),
        (SECRET, EXAMPLE_BODY, None),
    ],
)
def test_bad_signatures_are_rejected(secret: str, body: bytes, header: str | None) -> None:
    assert not signature_valid(secret, body, header)


# ------------------------------------------------------------------ routing


@pytest.mark.parametrize(
    ("event", "action", "handler"),
    [
        ("installation", "created", "_installation_changed"),
        ("installation", "deleted", "_installation_deleted"),
        ("installation", "suspend", "_installation_changed"),
        ("installation", "unsuspend", "_installation_changed"),
        ("installation", "new_permissions_accepted", "_installation_changed"),
        ("installation_repositories", "added", "_installation_changed"),
        ("installation_repositories", "removed", "_installation_changed"),
        ("push", None, "_store_only"),
        ("pull_request", "opened", "_store_only"),
        ("pull_request", "synchronize", "_store_only"),
        ("installation", "renamed", None),
        ("star", "created", None),
        ("ping", None, None),
    ],
)
def test_routing_table(event: str, action: str | None, handler: str | None) -> None:
    found = handler_for(event, action)
    assert (found.__name__ if found else None) == handler


# ------------------------------------------------------------------ inbox


def _payload(action: str | None = "deleted", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "installation": {
            "id": INSTALLATION,
            "account": {"login": "acme-gh", "id": 5150, "type": "Organization"},
            "target_type": "Organization",
            "repository_selection": "selected",
            "permissions": {"contents": "read", "metadata": "read"},
            "events": ["push"],
            "suspended_at": None,
        },
        "sender": {"login": "octo-admin", "id": 1},
        **extra,
    }
    if action is not None:
        body["action"] = action
    return body


@pytest.mark.integration
async def test_app_role_inserts_once_and_cannot_read(db_session: AsyncSession) -> None:
    first = await record_delivery(
        db_session, delivery_id="d-1", event="installation", payload=_payload()
    )
    again = await record_delivery(
        db_session, delivery_id="d-1", event="installation", payload=_payload()
    )
    assert isinstance(first, uuid.UUID)
    assert again is None
    for statement in (
        "SELECT payload FROM webhook_deliveries",
        "UPDATE webhook_deliveries SET status = 'processed'",
        "DELETE FROM webhook_deliveries",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db_session.begin_nested():
                await db_session.execute(text(statement))


@pytest.mark.integration
async def test_recorded_fields(system_session: AsyncSession) -> None:
    row_id = await record_delivery(
        system_session,
        delivery_id="d-2",
        event="installation",
        payload=_payload(note="nul \x00 here"),
        contains_nul=True,
    )
    row = await system_session.get(WebhookDelivery, row_id)
    assert row is not None
    assert (row.provider, row.event, row.action, row.installation_id) == (
        "github",
        "installation",
        "deleted",
        INSTALLATION,
    )
    assert (row.status, row.attempts, row.processed_at) == (DeliveryStatus.RECEIVED, 0, None)
    assert row.payload["note"] == "nul � here"


# ------------------------------------------------------------------ processing


async def _linked(db: AsyncSession) -> GitHubInstallation:
    org = Org(slug=f"org-{uuid.uuid4().hex[:8]}", name="Acme")
    db.add(org)
    await db.flush()
    row = GitHubInstallation(
        org_id=org.id,
        installation_id=INSTALLATION,
        account_login="acme-gh",
        account_type="Organization",
        account_id=5150,
        repository_selection="selected",
        permissions={"contents": "read"},
        events=["push"],
    )
    db.add(row)
    await db.flush()
    return row


class FakeGitHub:
    """GitHub's current view of the installation (None: uninstalled)."""

    def __init__(self, state: dict[str, Any] | None) -> None:
        self.state = state

    async def get_installation(self, installation_id: int) -> dict[str, Any]:
        if self.state is None:
            raise GitHubError(404, "Not Found")
        return {**self.state, "id": installation_id}


async def _deliver(
    db: AsyncSession,
    redis: FakeAsyncRedis,
    event: str,
    payload: dict[str, Any],
    app: FakeGitHub | None = None,
) -> tuple[WebhookDelivery, webhooks.ProcessResult]:
    row_id = await record_delivery(db, delivery_id=uuid.uuid4().hex, event=event, payload=payload)
    assert row_id is not None
    result = await process_delivery(db, redis, row_id, app=app)
    delivery = await db.get(WebhookDelivery, row_id, populate_existing=True)
    assert delivery is not None
    return delivery, result


@pytest.mark.integration
async def test_uninstall_marks_the_link_and_forgets_tokens(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    link = await _linked(system_session)
    await cast(
        "Awaitable[int]",
        fake_redis.sadd(f"github:itoken:{INSTALLATION}:keys", f"github:itoken:{INSTALLATION}:all"),
    )
    await fake_redis.set(f"github:itoken:{INSTALLATION}:all", "sealed")
    delivery, result = await _deliver(system_session, fake_redis, "installation", _payload())
    assert result == webhooks.ProcessResult(DeliveryStatus.PROCESSED)
    assert (delivery.status, delivery.attempts) == (DeliveryStatus.PROCESSED, 1)
    assert delivery.processed_at is not None
    await system_session.refresh(link)
    assert link.deleted_at is not None
    assert await fake_redis.exists(f"github:itoken:{INSTALLATION}:all") == 0

    event = await system_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "github.install.deleted")
    )
    assert event is not None
    assert (event.org_id, event.actor_type, event.actor_user_id) == (
        link.org_id,
        ActorType.SYSTEM,
        None,
    )
    assert event.meta["github_sender"] == "octo-admin"

    # Processing again is a no-op.
    again = await process_delivery(system_session, fake_redis, delivery.id)
    assert again.status is DeliveryStatus.PROCESSED
    await system_session.refresh(delivery)
    assert delivery.attempts == 1


@pytest.mark.integration
async def test_suspend_and_unsuspend(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    link = await _linked(system_session)
    suspended = _payload("suspend")
    suspended["installation"]["suspended_at"] = "2026-10-19T10:00:00Z"
    await _deliver(system_session, fake_redis, "installation", suspended)
    await system_session.refresh(link)
    assert link.suspended_at is not None
    assert link.suspended_at.isoformat() == "2026-10-19T10:00:00+00:00"
    await _deliver(system_session, fake_redis, "installation", _payload("unsuspend"))
    await system_session.refresh(link)
    assert link.suspended_at is None
    actions = (
        await system_session.scalars(
            select(AuditEvent.action)
            .where(AuditEvent.org_id == link.org_id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
    ).all()
    assert actions == ["github.install.suspended", "github.install.unsuspended"]


@pytest.mark.integration
async def test_new_permissions_and_repository_changes_update_the_link(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    link = await _linked(system_session)
    accepted = _payload("new_permissions_accepted")
    accepted["installation"]["permissions"] = {"contents": "read", "pull_requests": "write"}
    accepted["installation"]["events"] = ["push", "pull_request"]
    await _deliver(system_session, fake_redis, "installation", accepted)
    # The payload's installation object is the current one (its permissions included).
    added = _payload("added", repository_selection="all", repositories_added=[{"id": 1}])
    added["installation"] = {**accepted["installation"], "repository_selection": "all"}
    await _deliver(system_session, fake_redis, "installation_repositories", added)
    await system_session.refresh(link)
    assert link.permissions == {"contents": "read", "pull_requests": "write"}
    assert link.events == ["push", "pull_request"]
    assert link.repository_selection == "all"


@pytest.mark.integration
async def test_events_out_of_order_still_end_in_githubs_state(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    """Suspended then unsuspended on GitHub, but the unsuspend is processed first."""
    link = await _linked(system_session)
    github = FakeGitHub(_payload()["installation"])  # now: active
    await _deliver(system_session, fake_redis, "installation", _payload("unsuspend"), github)
    stale = _payload("suspend")
    stale["installation"]["suspended_at"] = "2026-10-19T10:00:00Z"
    await _deliver(system_session, fake_redis, "installation", stale, github)
    await system_session.refresh(link)
    assert link.suspended_at is None  # GitHub says active, whatever the late event said

    github.state = {**_payload()["installation"], "suspended_at": "2026-10-19T11:00:00Z"}
    await _deliver(system_session, fake_redis, "installation", _payload("unsuspend"), github)
    await system_session.refresh(link)
    assert link.suspended_at is not None


@pytest.mark.integration
async def test_an_event_for_an_uninstalled_app_marks_it_deleted(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    link = await _linked(system_session)
    await _deliver(
        system_session, fake_redis, "installation", _payload("suspend"), FakeGitHub(None)
    )
    await system_session.refresh(link)
    assert link.deleted_at is not None


@pytest.mark.integration
async def test_handler_errors_are_redacted(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def leaky(_: HandlerContext) -> DeliveryStatus:
        msg = "GET https://x.example/?token=ghs_abcdefghijklmnopqrstuvwxyz0123 failed"
        raise RuntimeError(msg)

    monkeypatch.setitem(webhooks.HANDLERS, ("installation", "deleted"), leaky)
    delivery, _ = await _deliver(system_session, fake_redis, "installation", _payload())
    assert delivery.error is not None
    assert "ghs_abcdefghij" not in delivery.error


@pytest.mark.integration
async def test_created_refreshes_an_existing_link(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    link = await _linked(system_session)
    created = _payload("created")
    created["installation"]["repository_selection"] = "all"
    delivery, _ = await _deliver(system_session, fake_redis, "installation", created)
    await system_session.refresh(link)
    assert (delivery.status, link.repository_selection) == (DeliveryStatus.PROCESSED, "all")


@pytest.mark.integration
async def test_events_for_unlinked_installations_are_harmless(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    for action in ("created", "deleted", "suspend", "unsuspend", "new_permissions_accepted"):
        delivery, _ = await _deliver(system_session, fake_redis, "installation", _payload(action))
        assert delivery.status is DeliveryStatus.PROCESSED


@pytest.mark.integration
@pytest.mark.parametrize(
    ("event", "action", "status"),
    [
        ("push", None, DeliveryStatus.STORED),
        ("pull_request", "opened", DeliveryStatus.STORED),
        ("star", "created", DeliveryStatus.IGNORED),
        ("ping", None, DeliveryStatus.IGNORED),
    ],
)
async def test_stored_and_ignored_events(
    event: str,
    action: str | None,
    status: DeliveryStatus,
    system_session: AsyncSession,
    fake_redis: FakeAsyncRedis,
) -> None:
    delivery, result = await _deliver(system_session, fake_redis, event, _payload(action))
    assert (delivery.status, result.status) == (status, status)


@pytest.mark.integration
async def test_failures_are_recorded_and_retried(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = await _linked(system_session)
    calls = 0

    async def flaky(ctx: HandlerContext) -> DeliveryStatus:
        nonlocal calls
        calls += 1
        row = await ctx.installation()
        assert row is not None
        row.account_login = "half-written"
        await ctx.db.flush()
        if calls == 1:
            msg = "github hiccup"
            raise RuntimeError(msg)
        return DeliveryStatus.PROCESSED

    monkeypatch.setitem(webhooks.HANDLERS, ("installation", "deleted"), flaky)
    delivery, result = await _deliver(system_session, fake_redis, "installation", _payload())
    assert result.status is DeliveryStatus.FAILED
    assert isinstance(result.error, RuntimeError)
    assert delivery.error == "RuntimeError: github hiccup"
    assert delivery.processed_at is None
    await system_session.refresh(link)
    assert link.account_login == "acme-gh"  # the failed attempt was rolled back

    retry = await process_delivery(system_session, fake_redis, delivery.id)
    await system_session.refresh(delivery)
    assert (retry.status, delivery.status, delivery.attempts, delivery.error) == (
        DeliveryStatus.PROCESSED,
        DeliveryStatus.PROCESSED,
        2,
        None,
    )


@pytest.mark.integration
async def test_unknown_delivery(system_session: AsyncSession, fake_redis: FakeAsyncRedis) -> None:
    assert (await process_delivery(system_session, fake_redis, uuid.uuid4())).status is None


@pytest.mark.integration
async def test_a_delivery_held_by_one_worker_is_skipped_by_another(
    migrated_test_db: Settings, fake_redis: FakeAsyncRedis
) -> None:
    """Two workers with the same delivery: the second sees the row locked and leaves it."""
    database = make_system_database(migrated_test_db.database)
    row_id: uuid.UUID | None = None
    try:
        async with database.session() as db:
            row_id = await record_delivery(
                db, delivery_id=f"lock-{uuid.uuid4()}", event="ping", payload={}
            )
        assert row_id is not None
        async with database.session() as holder:
            await holder.execute(
                select(WebhookDelivery).where(WebhookDelivery.id == row_id).with_for_update()
            )
            async with database.session() as other:
                skipped = await process_delivery(other, fake_redis, row_id)
            assert skipped.status is None
        async with database.session() as db:
            done = await process_delivery(db, fake_redis, row_id)
        assert done.status is DeliveryStatus.IGNORED
    finally:
        async with database.session() as db:
            await db.execute(delete(WebhookDelivery).where(WebhookDelivery.id == row_id))
        await database.dispose()
