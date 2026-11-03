"""Notifications over HTTP (8.10): a person's inbox in an org (only theirs; read one, or all),
its live stream, their preferences (the same in every org), and the org's Slack (admins; only
Slack's webhooks, kept encrypted, never shown back). An item about a repository is shown only to
whoever can read the repository on GitHub."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api import stream_access, streams
from sensei_api.repos import get_repo_access
from sensei_api.routers import notifications as notifications_router
from sensei_core.config import Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.oauth import OAuthError
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AuditEvent,
    Membership,
    NotificationKind,
    Repo,
    RepoSource,
    Role,
    SlackIntegration,
    User,
)
from sensei_core.notifications.deliver import slack_context
from sensei_core.notifications.service import Event, notify, publish
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
HOOK = "https://hooks.slack.com/services/T0001/B0002/abcDEF1234"


@pytest.fixture
def app(api_app: FastAPI, migrated_test_db: Settings, fake_redis: FakeAsyncRedis,
        db_session: AsyncSession) -> FastAPI:  # fmt: skip
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)

    @asynccontextmanager
    async def same_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    api_app.state.stream_sessions = same_session
    return api_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        app.state.http = http
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test:
            yield test


class FakeReaders:
    """What GitHub says each person can read (``lapsed``: their sign-in must be renewed)."""

    def __init__(self) -> None:
        self.readable: set[uuid.UUID] = set()
        self.lapsed = False

    async def readable_for(self, user: User, repos: list[Repo]) -> list[Repo]:
        if self.lapsed:
            raise OAuthError("github_reauth_required", "Sign in with GitHub again.")
        return [repo for repo in repos if repo.id in self.readable]


@pytest.fixture
def readers(app: FastAPI) -> Generator[FakeReaders]:
    fake = FakeReaders()

    class Access:
        async def readable(self, user: User, repos: list[Repo]) -> list[Repo]:
            return await fake.readable_for(user, repos)

    app.dependency_overrides[get_repo_access] = Access
    yield fake
    app.dependency_overrides.pop(get_repo_access, None)


async def _repo(db: AsyncSession, org_id: uuid.UUID, slug: str, *, public: bool = False) -> Repo:
    repo = Repo(
        org_id=org_id, source=RepoSource.PUBLIC_URL if public else RepoSource.GITHUB_APP,
        github_repo_id=hash(slug) % 10_000, owner="o", name=slug, full_name=f"o/{slug}",
        slug=slug, default_branch="main", visibility="public" if public else "private",
    )  # fmt: skip
    db.add(repo)
    await db.flush()
    return repo


async def _org(db: AsyncSession) -> tuple[uuid.UUID, User, User, User]:
    """Acme: its owner, a member and a viewer."""
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "http://localhost:3000").create(
        db, owner, name="Acme", slug="acme"
    )
    member, viewer = await make_user(db), await make_user(db)
    db.add(Membership(org_id=access.org.id, user_id=member.id, role=Role.MEMBER))
    db.add(Membership(org_id=access.org.id, user_id=viewer.id, role=Role.VIEWER))
    await db.flush()
    return access.org.id, owner, member, viewer


async def _tell(db: AsyncSession, org_id: uuid.UUID, key: str, users: list[User],
                kind: NotificationKind = NotificationKind.FIX_FAILED,
                meta: dict[str, str] | None = None) -> None:  # fmt: skip
    await set_org_context(db, org_id)
    event = Event(org_id, kind, key, f"Fix failed: {key}", "In o/items.", "/acme/items/fixes/1",
                  meta)  # fmt: skip
    await notify(db, event, [u.id for u in users])


async def test_a_persons_inbox_newest_first_read_one_or_all(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org_id, owner, member, viewer = await _org(db_session)
    for key in ("a", "b"):
        await _tell(db_session, org_id, key, [member, viewer])
    await _tell(db_session, org_id, "c", [member, viewer], NotificationKind.APPROVAL_NEEDED,
                {"risk": "high"})  # fmt: skip
    await _tell(db_session, org_id, "owner-only", [owner])
    _, headers = await sign_in(app, client, db_session, viewer)

    page = await client.get(f"{API}/orgs/acme/inbox", params={"limit": 2})
    assert page.status_code == 200
    body = page.json()
    assert [i["title"] for i in body["items"]] == ["Fix failed: c", "Fix failed: b"]
    assert body["unread"] == 3
    assert body["items"][0]["link"] == "/acme/items/fixes/1"
    assert [(i["kind"], i["risk"]) for i in body["items"]] == [
        ("approval_needed", "high"), ("fix_failed", None),
    ]  # fmt: skip
    older = await client.get(f"{API}/orgs/acme/inbox", params={"before": body["next_cursor"]})
    assert [i["title"] for i in older.json()["items"]] == ["Fix failed: a"]
    # Read one (someone else's id is passed over), then the rest.
    owners = (await client.get(f"{API}/orgs/acme/inbox")).json()["items"]
    first = body["items"][0]["id"]
    read = await client.post(f"{API}/orgs/acme/inbox/read", json={"ids": [first]},
                             headers=headers)  # fmt: skip
    assert read.json() == {"unread": 2}
    unread = (await client.get(f"{API}/orgs/acme/inbox", params={"unread": True})).json()
    assert [i["title"] for i in unread["items"]] == ["Fix failed: b", "Fix failed: a"]
    assert all(not i["read"] for i in owners)
    everything = await client.post(f"{API}/orgs/acme/inbox/read", json={}, headers=headers)
    assert everything.json() == {"unread": 0}
    # The member's are their own: still unread.
    _, member_headers = await sign_in(app, client, db_session, member)
    theirs = (await client.get(f"{API}/orgs/acme/inbox")).json()
    assert (len(theirs["items"]), theirs["unread"]) == (3, 3)
    stolen = await client.post(f"{API}/orgs/acme/inbox/read", json={"ids": [first]},
                               headers=member_headers)  # fmt: skip
    assert stolen.json() == {"unread": 3}


async def test_someone_outside_the_org_has_no_inbox_there(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _org(db_session)
    await sign_in(app, client, db_session)
    response = await client.get(f"{API}/orgs/acme/inbox")
    assert response.status_code == 404


async def test_the_inbox_stream_sends_what_arrives_for_that_person(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streams, "READ_BLOCK_MS", 50)
    monkeypatch.setattr(notifications_router, "MEMBERSHIP_RECHECK_SECONDS", 0.0)
    org_id, owner, member, _ = await _org(db_session)
    await set_org_context(db_session, org_id)
    event = Event(org_id, NotificationKind.PR_OPENED, "fix:1:pr", "Draft pull request opened",
                  "", "/acme/items/fixes/1")  # fmt: skip
    await publish(fake_redis, [await notify(db_session, event, [member.id])])
    await publish(fake_redis, [await notify(db_session, Event(
        org_id, NotificationKind.FIX_FAILED, "fix:2:failed", "Someone else's", "", None,
    ), [owner.id])])  # fmt: skip
    await sign_in(app, client, db_session, member)

    calls = 0

    async def twice(*_args: object) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1  # then the session ended: the stream closes

    monkeypatch.setattr(streams, "still_member", twice)
    response = await client.get(f"{API}/orgs/acme/inbox/events", params={"last_event_id": "0"})

    assert response.status_code == 200
    events = [e for e in _sse(response.text) if "event" in e]
    assert [e["event"] for e in events] == ["inbox.new"]
    assert '"title":"Draft pull request opened"' in events[0]["data"]
    assert calls == 2


async def test_the_inbox_stream_drops_what_its_reader_cant_read(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streams, "READ_BLOCK_MS", 50)
    monkeypatch.setattr(notifications_router, "MEMBERSHIP_RECHECK_SECONDS", 0.0)
    org_id, _, member, _ = await _org(db_session)
    await set_org_context(db_session, org_id)
    seen, hidden = await _repo(db_session, org_id, "seen"), await _repo(db_session, org_id, "hid")
    for repo in (seen, hidden):
        event = Event(org_id, NotificationKind.FIX_FAILED, f"fix:{repo.slug}:failed",
                      f"Fix failed in {repo.slug}", "", None, repo_id=repo.id)  # fmt: skip
        await publish(fake_redis, [await notify(db_session, event, [member.id])])
    await sign_in(app, client, db_session, member)
    asked: list[str] = []

    async def can_read(_request: object, _oauth: object, _org: object, _user: object,
                       repo_id: str) -> bool:  # fmt: skip
        asked.append(repo_id)
        return repo_id == str(seen.id)

    monkeypatch.setattr(stream_access, "_can_read", can_read)
    calls = 0

    async def twice(*_args: object) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    monkeypatch.setattr(streams, "still_member", twice)
    response = await client.get(f"{API}/orgs/acme/inbox/events", params={"last_event_id": "0"})

    titles = [e["data"] for e in _sse(response.text) if e.get("event") == "inbox.new"]
    assert len(titles) == 1
    assert "Fix failed in seen" in titles[0]
    assert sorted(asked) == sorted([str(seen.id), str(hidden.id)])


async def test_items_about_a_repository_are_shown_only_to_who_can_read_it(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, readers: FakeReaders
) -> None:
    org_id, _, member, _ = await _org(db_session)
    await set_org_context(db_session, org_id)
    seen, hidden = await _repo(db_session, org_id, "seen"), await _repo(db_session, org_id, "hid")
    public = await _repo(db_session, org_id, "open", public=True)
    for key, repo in (("s", seen), ("h", hidden), ("p", public), ("none", None)):
        event = Event(org_id, NotificationKind.FIX_FAILED, key, f"Fix failed: {key}", "", None,
                      repo_id=repo.id if repo else None)  # fmt: skip
        await notify(db_session, event, [member.id])
    readers.readable = {seen.id, public.id}
    _, headers = await sign_in(app, client, db_session, member)

    body = (await client.get(f"{API}/orgs/acme/inbox")).json()
    assert sorted(i["title"] for i in body["items"]) == [
        "Fix failed: none", "Fix failed: p", "Fix failed: s",
    ]  # fmt: skip
    assert body["unread"] == 3
    # Read all: what they can't see is left alone, and isn't counted.
    marked = await client.post(f"{API}/orgs/acme/inbox/read", json={}, headers=headers)
    assert marked.json() == {"unread": 0}
    # Their GitHub sign-in lapsed: only what isn't a private repository's.
    readers.lapsed = True
    lapsed = (await client.get(f"{API}/orgs/acme/inbox")).json()
    assert sorted(i["title"] for i in lapsed["items"]) == ["Fix failed: none", "Fix failed: p"]
    # A repository removed takes its items with it.
    readers.lapsed = False
    readers.readable = {hidden.id, seen.id, public.id}
    shown = (await client.get(f"{API}/orgs/acme/inbox")).json()
    assert (len(shown["items"]), shown["unread"]) == (4, 1)
    await db_session.delete(hidden)
    await db_session.flush()
    after = (await client.get(f"{API}/orgs/acme/inbox")).json()
    assert len(after["items"]) == 3


async def test_the_inbox_by_kind_and_marking_one_kind_read(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org_id, _, member, _ = await _org(db_session)
    await _tell(db_session, org_id, "approve", [member], NotificationKind.APPROVAL_NEEDED)
    await _tell(db_session, org_id, "failed", [member])
    await _tell(db_session, org_id, "pr", [member], NotificationKind.PR_OPENED)
    _, headers = await sign_in(app, client, db_session, member)

    kinds = {"kinds": ["approval_needed", "pr_opened"]}
    some = (await client.get(f"{API}/orgs/acme/inbox", params=kinds)).json()
    assert sorted(i["kind"] for i in some["items"]) == ["approval_needed", "pr_opened"]
    assert some["unread"] == 3  # in all
    marked = await client.post(f"{API}/orgs/acme/inbox/read",
                               json={"kinds": ["approval_needed"]}, headers=headers)  # fmt: skip
    assert marked.json() == {"unread": 2}
    left = (await client.get(f"{API}/orgs/acme/inbox", params={"unread": True})).json()
    assert sorted(i["kind"] for i in left["items"]) == ["fix_failed", "pr_opened"]


def _sse(body: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line and not line.startswith(":"):
                name, _, value = line.partition(":")
                fields[name] = value.lstrip(" ")
        if fields:
            events.append(fields)
    return events


async def test_preferences_are_a_persons_and_change_per_kind(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await sign_in(app, client, db_session)

    defaults = (await client.get(f"{API}/me/notifications")).json()
    by_kind = {k["kind"]: k for k in defaults["kinds"]}
    assert set(by_kind) == {kind.value for kind in NotificationKind}
    assert (by_kind["index_done"]["in_app"], by_kind["index_done"]["email"]) == (True, False)
    assert by_kind["approval_needed"]["label"] == "Approvals needed"
    assert defaults["email"] is not None

    changed = await client.patch(f"{API}/me/notifications", headers=headers, json={
        "kinds": {"pr_opened": {"email": False}, "index_done": {"email": True, "in_app": False}},
    })  # fmt: skip
    assert changed.status_code == 200
    after = {k["kind"]: (k["in_app"], k["email"]) for k in changed.json()["kinds"]}
    assert after["pr_opened"] == (True, False)
    assert after["index_done"] == (False, True)
    assert after["fix_failed"] == (True, True)  # untouched
    again = await client.patch(f"{API}/me/notifications", headers=headers,
                               json={"kinds": {"pr_opened": {"in_app": False}}})  # fmt: skip
    assert {k["kind"]: (k["in_app"], k["email"]) for k in again.json()["kinds"]}["pr_opened"] == (
        False, False
    )  # fmt: skip
    unknown = await client.patch(f"{API}/me/notifications", headers=headers,
                                 json={"kinds": {"weather": {"email": True}}})  # fmt: skip
    assert unknown.status_code == 422


async def test_the_orgs_slack_is_admins_only_slacks_own_and_never_shown_back(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    org_id, owner, member, _ = await _org(db_session)
    _, member_headers = await sign_in(app, client, db_session, member)
    refused = await client.put(f"{API}/orgs/acme/integrations/slack", headers=member_headers,
                               json={"webhook_url": HOOK, "kinds": ["fix_failed"]})  # fmt: skip
    assert refused.status_code == 403
    _, headers = await sign_in(app, client, db_session, owner)
    url = f"{API}/orgs/acme/integrations/slack"
    assert (await client.get(url)).status_code == 404
    for bad in ("https://evil.example/hook", "http://hooks.slack.com/services/T1/B2/c"):
        wrong = await client.put(url, headers=headers, json={"webhook_url": bad})
        assert (wrong.status_code, wrong.json()["error"]["code"]) == (422, "slack_webhook")
    nothing = await client.put(url, headers=headers, json={"kinds": ["fix_failed"]})
    assert nothing.status_code == 422  # connecting needs the webhook

    connected = await client.put(url, headers=headers, json={
        "webhook_url": HOOK, "channel": "#fixes", "kinds": ["fix_failed", "pr_opened", "pr_opened"],
    })  # fmt: skip
    assert connected.status_code == 200
    assert connected.json() == {"channel": "#fixes", "kinds": ["fix_failed", "pr_opened"],
                                "webhook_hint": "…1234"}  # fmt: skip
    assert HOOK not in connected.text
    await set_org_context(db_session, org_id)
    row = await db_session.scalar(select(SlackIntegration).where(SlackIntegration.org_id == org_id))
    assert row is not None
    assert HOOK.encode() not in row.webhook_url_enc
    cipher = app.state.token_cipher
    assert cipher.decrypt_text(row.webhook_url_enc, context=slack_context(org_id)) == HOOK
    # Changed without the webhook: it stays.
    kinds = await client.put(url, headers=headers, json={"channel": "#ops", "kinds": ["cost_cap"]})
    assert kinds.json() == {"channel": "#ops", "kinds": ["cost_cap"], "webhook_hint": "…1234"}
    assert (await client.get(url)).json() == kinds.json()
    assert (await client.delete(url, headers=headers)).status_code == 204
    assert (await client.get(url)).status_code == 404
    actions = (await db_session.scalars(
        select(AuditEvent.action).where(AuditEvent.org_id == org_id,
                                        AuditEvent.action.like("slack.%"))
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )).all()  # fmt: skip
    assert actions == ["slack.connect", "slack.connect", "slack.disconnect"]
    metas = (await db_session.scalars(
        select(AuditEvent.meta).where(AuditEvent.action == "slack.connect")
    )).all()  # fmt: skip
    assert all(HOOK not in str(meta) for meta in metas)
