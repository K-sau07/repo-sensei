"""Audit log: one row per privileged action, request context, isolation and append-only rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit, list_org_audit
from sensei_core.context import bind_client_ip, bind_context
from sensei_core.db.base import new_id
from sensei_core.db.tenancy import set_org_context, set_user_context
from sensei_core.errors import ForbiddenError
from sensei_core.mail import MemoryMailer
from sensei_core.models import ActorType, AuditEvent, Membership, Role, User
from sensei_core.orgs.service import OrgAccess, OrgService
from sensei_testing import make_user

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def service() -> OrgService:
    return OrgService(MemoryMailer(), "https://app.example.com", clock=Clock())


async def _events(db: AsyncSession, org_id: uuid.UUID) -> list[AuditEvent]:
    await set_org_context(db, org_id)
    result = await db.scalars(
        select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
    )
    return list(result.all())


async def _org(db: AsyncSession, service: OrgService) -> tuple[OrgAccess, User]:
    owner = await make_user(db)
    access = await service.create(db, owner, name="Acme", slug=f"acme-{owner.github_id}")
    return access, owner


async def _member(db: AsyncSession, access: OrgAccess, role: Role) -> User:
    user = await make_user(db)
    db.add(Membership(org_id=access.org.id, user_id=user.id, role=role))
    await db.flush()
    return user


async def _actions_after(
    db: AsyncSession, access: OrgAccess, seen: int
) -> list[tuple[str, str | None, dict[str, object]]]:
    events = (await _events(db, access.org.id))[seen:]
    return [(e.action, e.target_id, e.meta) for e in events]


# ------------------------------------------------------------------ one row per action


async def test_org_create_is_audited(db_session: AsyncSession, service: OrgService) -> None:
    access, owner = await _org(db_session, service)
    [event] = await _events(db_session, access.org.id)
    assert event.action == "org.create"
    assert (event.actor_user_id, event.actor_type) == (owner.id, ActorType.USER)
    assert (event.target_type, event.target_id) == ("org", str(access.org.id))
    assert event.meta == {"slug": access.org.slug, "name": "Acme"}


async def test_org_update_records_changes_and_skips_no_ops(
    db_session: AsyncSession, service: OrgService
) -> None:
    access, _ = await _org(db_session, service)
    await service.update(db_session, access, name="Acme", slug=None)  # nothing changes
    old_slug = access.org.slug
    await service.update(db_session, access, name="Acme 2", slug="acme-two")
    assert await _actions_after(db_session, access, 1) == [
        (
            "org.update",
            str(access.org.id),
            {"name": ["Acme", "Acme 2"], "slug": [old_slug, "acme-two"]},
        )
    ]


async def test_org_delete_is_audited(db_session: AsyncSession, service: OrgService) -> None:
    access, _ = await _org(db_session, service)
    await service.delete(db_session, access)
    assert await _actions_after(db_session, access, 1) == [("org.delete", str(access.org.id), {})]


async def test_member_changes_are_audited(db_session: AsyncSession, service: OrgService) -> None:
    access, _ = await _org(db_session, service)
    mate = await _member(db_session, access, Role.MEMBER)
    leaver = await _member(db_session, access, Role.VIEWER)

    await service.change_role(db_session, access, mate.id, Role.MEMBER)  # same role: no row
    await service.change_role(db_session, access, mate.id, Role.ADMIN)
    await service.remove_member(db_session, access, mate.id)
    leaver_access = await service.access(db_session, leaver, access.org.slug)
    await service.leave(db_session, leaver_access)

    assert await _actions_after(db_session, access, 1) == [
        ("member.role_change", str(mate.id), {"from": "member", "to": "admin"}),
        ("member.remove", str(mate.id), {"role": "admin"}),
        ("member.leave", str(leaver.id), {"role": "viewer"}),
    ]
    events = await _events(db_session, access.org.id)
    assert events[-1].actor_user_id == leaver.id


async def test_invitation_lifecycle_is_audited(
    db_session: AsyncSession, service: OrgService
) -> None:
    access, _ = await _org(db_session, service)
    invitation, token = await service.invite(
        db_session, access, role=Role.MEMBER, email="friend@example.com"
    )
    revoked, _ = await service.invite(db_session, access, role=Role.VIEWER, github_login="@octo")
    await service.revoke_invitation(db_session, access, revoked.id)
    await service.revoke_invitation(db_session, access, revoked.id)  # already revoked: no row

    friend = await make_user(db_session, email="friend@example.com")
    await service.accept_invitation(db_session, friend, token)

    assert await _actions_after(db_session, access, 1) == [
        (
            "invitation.create",
            str(invitation.id),
            {"role": "member", "email": "friend@example.com", "github_login": None},
        ),
        (
            "invitation.create",
            str(revoked.id),
            {"role": "viewer", "email": None, "github_login": "octo"},
        ),
        ("invitation.revoke", str(revoked.id), {}),
        ("invitation.accept", str(invitation.id), {"role": "member"}),
    ]
    events = await _events(db_session, access.org.id)
    assert events[-1].actor_user_id == friend.id


async def test_refused_actions_write_nothing(db_session: AsyncSession, service: OrgService) -> None:
    access, _ = await _org(db_session, service)
    viewer = await _member(db_session, access, Role.VIEWER)
    viewer_access = await service.access(db_session, viewer, access.org.slug)
    with pytest.raises(ForbiddenError):
        await service.update(db_session, viewer_access, name="Hijacked", slug=None)
    assert await _actions_after(db_session, access, 1) == []


# ------------------------------------------------------------------ context and safety


async def test_request_context_and_redaction(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    with bind_context(request_id="req-123"), bind_client_ip("203.0.113.9"):
        await audit(
            db_session,
            AuditAction.LOGIN,
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            meta={"access_token": "ghu_abcdefghijklmnopqrstuvwxyz", "method": "github"},
        )
    event = await db_session.scalar(select(AuditEvent).where(AuditEvent.actor_user_id == user.id))
    assert event is not None
    assert (event.org_id, event.request_id, str(event.ip)) == (None, "req-123", "203.0.113.9")
    assert event.meta == {"access_token": "***", "method": "github"}


async def test_system_events_have_no_actor(db_session: AsyncSession, service: OrgService) -> None:
    access, _ = await _org(db_session, service)
    await audit(db_session, AuditAction.ORG_DELETE, actor_user_id=None, org_id=access.org.id)
    event = (await _events(db_session, access.org.id))[-1]
    assert (event.actor_type, event.actor_user_id) == (ActorType.SYSTEM, None)


async def test_account_events_are_private_to_their_user(db_session: AsyncSession) -> None:
    alice, bob = await make_user(db_session), await make_user(db_session)
    await audit(db_session, AuditAction.LOGIN, actor_user_id=alice.id)

    await set_user_context(db_session, bob.id)
    assert await db_session.scalar(select(AuditEvent.id).where(AuditEvent.org_id.is_(None))) is None
    await set_user_context(db_session, alice.id)
    [mine] = (await db_session.scalars(select(AuditEvent))).all()
    assert mine.actor_user_id == alice.id

    # Nobody can write an account event on someone else's behalf.
    await set_user_context(db_session, bob.id)
    forged = insert(AuditEvent).values(
        id=new_id(), actor_user_id=alice.id, actor_type=ActorType.USER, action="auth.login"
    )
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db_session.begin_nested():
            await db_session.execute(forged)


async def test_other_orgs_audit_is_invisible(db_session: AsyncSession, service: OrgService) -> None:
    first, _ = await _org(db_session, service)
    second, _ = await _org(db_session, service)
    await set_org_context(db_session, second.org.id)
    visible = (await db_session.scalars(select(AuditEvent.org_id))).all()
    assert set(visible) == {second.org.id}
    assert first.org.id not in visible


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"],
)
async def test_audit_log_is_append_only(
    db_session: AsyncSession, service: OrgService, statement: str
) -> None:
    await _org(db_session, service)
    with pytest.raises(DBAPIError, match="permission denied"):
        async with db_session.begin_nested():
            await db_session.execute(text(statement))


# ------------------------------------------------------------------ listing


async def test_listing_pages_newest_first_and_filters(
    db_session: AsyncSession, service: OrgService
) -> None:
    access, _ = await _org(db_session, service)
    for n in range(4):
        await service.invite(db_session, access, role=Role.MEMBER, email=f"p{n}@example.com")

    org_id = access.org.id
    page_one = await list_org_audit(db_session, org_id, limit=3)
    page_two = await list_org_audit(db_session, org_id, limit=3, before=page_one[-1][0].id)
    actions = [e.action for e, _ in page_one + page_two]
    assert actions == ["invitation.create"] * 4 + ["org.create"]
    ids = [e.id for e, _ in page_one + page_two]
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 5
    assert page_one[0][1] is not None  # actor login is joined in

    only_creates = await list_org_audit(db_session, org_id, action=AuditAction.ORG_CREATE)
    assert [e.action for e, _ in only_creates] == ["org.create"]

    other, _ = await _org(db_session, service)
    foreign_cursor = (await list_org_audit(db_session, other.org.id))[0][0].id
    await set_org_context(db_session, org_id)
    assert await list_org_audit(db_session, org_id, before=foreign_cursor) == []
