"""Org service: slugs, the role matrix, last-owner protection, invitations and isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
)
from sensei_core.mail import MemoryMailer
from sensei_core.models import Membership, Role, User
from sensei_core.orgs.service import OrgAccess, OrgService, validate_slug
from sensei_testing import make_user

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mailer() -> MemoryMailer:
    return MemoryMailer()


@pytest.fixture
def service(mailer: MemoryMailer, clock: Clock) -> OrgService:
    return OrgService(mailer, "https://app.example.com", clock=clock)


async def _org_with(
    db: AsyncSession, service: OrgService, roles: dict[str, Role]
) -> tuple[OrgAccess, dict[str, User]]:
    """An org owned by 'owner' plus extra members with the given roles."""
    owner = await make_user(db)
    access = await service.create(db, owner, name="Acme", slug=f"acme-{owner.github_id}")
    users = {"owner": owner}
    for name, role in roles.items():
        user = await make_user(db)
        db.add(Membership(org_id=access.org.id, user_id=user.id, role=role))
        users[name] = user
    await db.flush()
    return access, users


async def _as(db: AsyncSession, service: OrgService, user: User, access: OrgAccess) -> OrgAccess:
    return await service.access(db, user, access.org.slug)


# ------------------------------------------------------------------ slugs and orgs


@pytest.mark.parametrize("slug", ["acme", "acme-corp", "a1b", "x" * 39])
def test_valid_slugs(slug: str) -> None:
    assert validate_slug(slug.upper()) == slug.lower()


@pytest.mark.parametrize(
    ("slug", "code"),
    [
        ("ab", "invalid_slug"),
        ("x" * 40, "invalid_slug"),
        ("-acme", "invalid_slug"),
        ("acme-", "invalid_slug"),
        ("ac--me", "invalid_slug"),
        ("acme_corp", "invalid_slug"),
        ("api", "slug_reserved"),
        ("settings", "slug_reserved"),
    ],
)
def test_invalid_slugs(slug: str, code: str) -> None:
    with pytest.raises(InvalidError) as exc:
        validate_slug(slug)
    assert exc.value.code == code


async def test_creator_becomes_owner(db_session: AsyncSession, service: OrgService) -> None:
    user = await make_user(db_session)
    access = await service.create(db_session, user, name="  Acme  ", slug="Acme-Corp-1")
    assert (access.role, access.org.slug, access.org.name) == (Role.OWNER, "acme-corp-1", "Acme")
    assert user.last_org_slug == "acme-corp-1"


async def test_slug_must_be_unique(db_session: AsyncSession, service: OrgService) -> None:
    a, b = await make_user(db_session), await make_user(db_session)
    await service.create(db_session, a, name="One", slug="dupe-slug")
    with pytest.raises(ConflictError) as exc:
        await service.create(db_session, b, name="Two", slug="DUPE-slug")
    assert exc.value.code == "slug_taken"


async def test_users_only_see_their_own_orgs(db_session: AsyncSession, service: OrgService) -> None:
    alice, bob = await make_user(db_session), await make_user(db_session)
    await service.create(db_session, alice, name="Alpha", slug="alpha-org")
    await service.create(db_session, bob, name="Beta", slug="beta-org")
    assert [o.slug for o, _ in await service.list_for_user(db_session, alice)] == ["alpha-org"]
    assert [o.slug for o, _ in await service.list_for_user(db_session, bob)] == ["beta-org"]
    with pytest.raises(NotFoundError) as exc:
        await service.access(db_session, alice, "beta-org")
    assert exc.value.code == "org_not_found"


async def test_deleted_orgs_disappear(db_session: AsyncSession, service: OrgService) -> None:
    access, users = await _org_with(db_session, service, {})
    await service.delete(db_session, access)
    assert await service.list_for_user(db_session, users["owner"]) == []
    with pytest.raises(NotFoundError):
        await service.access(db_session, users["owner"], access.org.slug)


# ------------------------------------------------------------------ role matrix

MATRIX = [
    # action, actor role, allowed
    ("update", Role.VIEWER, False),
    ("update", Role.MEMBER, False),
    ("update", Role.ADMIN, True),
    ("delete", Role.ADMIN, False),
    ("delete", Role.OWNER, True),
    ("invite_member", Role.MEMBER, False),
    ("invite_member", Role.ADMIN, True),
    ("invite_owner", Role.ADMIN, False),
    ("invite_owner", Role.OWNER, True),
    ("promote_to_admin", Role.MEMBER, False),
    ("promote_to_admin", Role.ADMIN, True),
    ("promote_to_owner", Role.ADMIN, False),
    ("promote_to_owner", Role.OWNER, True),
    ("remove_member", Role.MEMBER, False),
    ("remove_member", Role.ADMIN, True),
    ("remove_owner", Role.ADMIN, False),
    ("remove_owner", Role.OWNER, True),
]


@pytest.mark.parametrize(("action", "actor_role", "allowed"), MATRIX)
async def test_role_matrix(
    action: str, actor_role: Role, allowed: bool, db_session: AsyncSession, service: OrgService
) -> None:
    access, users = await _org_with(
        db_session,
        service,
        {"actor": actor_role, "target": Role.MEMBER, "second_owner": Role.OWNER},
    )
    actor = await _as(db_session, service, users["actor"], access)
    target, second_owner = users["target"].id, users["second_owner"].id

    async def run() -> object:
        match action:
            case "update":
                return await service.update(db_session, actor, name="Renamed", slug=None)
            case "delete":
                return await service.delete(db_session, actor)
            case "invite_member":
                return await service.invite(db_session, actor, role=Role.MEMBER, email="a@x.io")
            case "invite_owner":
                return await service.invite(db_session, actor, role=Role.OWNER, email="b@x.io")
            case "promote_to_admin":
                return await service.change_role(db_session, actor, target, Role.ADMIN)
            case "promote_to_owner":
                return await service.change_role(db_session, actor, target, Role.OWNER)
            case "remove_member":
                return await service.remove_member(db_session, actor, target)
            case "remove_owner":
                return await service.remove_member(db_session, actor, second_owner)
            case _:
                raise AssertionError(action)

    if allowed:
        await run()
    else:
        with pytest.raises(ForbiddenError) as exc:
            await run()
        assert exc.value.code == "insufficient_role"
        assert exc.value.details is not None
        assert "required_role" in exc.value.details


# ------------------------------------------------------------------ last owner


async def test_last_owner_cannot_be_demoted_removed_or_leave(
    db_session: AsyncSession, service: OrgService
) -> None:
    access, users = await _org_with(db_session, service, {})
    owner_id = users["owner"].id
    attempts = [
        service.change_role(db_session, access, owner_id, Role.ADMIN),
        service.remove_member(db_session, access, owner_id),
        service.leave(db_session, access),
    ]
    for attempt in attempts:
        with pytest.raises(ConflictError) as exc:
            await attempt
        assert exc.value.code == "last_owner"


async def test_an_owner_can_leave_when_another_owner_exists(
    db_session: AsyncSession, service: OrgService
) -> None:
    access, users = await _org_with(db_session, service, {"co": Role.OWNER})
    await service.leave(db_session, access)
    remaining = await service.access(db_session, users["co"], access.org.slug)
    assert remaining.role is Role.OWNER


# ------------------------------------------------------------------ invitations


async def test_invitation_by_email_end_to_end(
    db_session: AsyncSession, service: OrgService, mailer: MemoryMailer
) -> None:
    access, _ = await _org_with(db_session, service, {})
    invitee = await make_user(db_session, email="New.Person@Example.com")
    _, token = await service.invite(
        db_session, access, role=Role.ADMIN, email="new.person@example.com"
    )

    assert len(mailer.sent) == 1
    assert mailer.sent[0].link == f"https://app.example.com/invite/{token}"

    preview = await service.preview_invitation(db_session, token)
    assert (preview.org_name, preview.role, preview.status) == ("Acme", Role.ADMIN, "pending")

    joined = await service.accept_invitation(db_session, invitee, token)
    assert joined.role is Role.ADMIN
    assert invitee.last_org_slug == access.org.slug
    assert (await service.preview_invitation(db_session, token)).status == "accepted"
    with pytest.raises(ConflictError) as exc:
        await service.accept_invitation(db_session, invitee, token)
    assert exc.value.code == "invitation_accepted"


async def test_invitation_by_github_login(db_session: AsyncSession, service: OrgService) -> None:
    access, _ = await _org_with(db_session, service, {})
    invitee = await make_user(db_session, login="OctoCat", email=None)
    _, token = await service.invite(db_session, access, role=Role.VIEWER, github_login="@octocat")
    joined = await service.accept_invitation(db_session, invitee, token)
    assert joined.role is Role.VIEWER


async def test_invitation_for_someone_else_is_refused(
    db_session: AsyncSession, service: OrgService
) -> None:
    access, _ = await _org_with(db_session, service, {})
    stranger = await make_user(db_session, email="stranger@example.com")
    _, token = await service.invite(
        db_session, access, role=Role.MEMBER, email="friend@example.com"
    )
    with pytest.raises(ForbiddenError) as exc:
        await service.accept_invitation(db_session, stranger, token)
    assert exc.value.code == "invitation_wrong_account"


async def test_revoked_and_expired_invitations(
    db_session: AsyncSession, service: OrgService, clock: Clock
) -> None:
    access, _ = await _org_with(db_session, service, {})
    invitee = await make_user(db_session, email="late@example.com")
    revoked, revoked_token = await service.invite(
        db_session, access, role=Role.MEMBER, email="late@example.com"
    )
    _, expiring_token = await service.invite(
        db_session, access, role=Role.MEMBER, email="late@example.com"
    )
    await service.revoke_invitation(db_session, access, revoked.id)
    clock.now = T0 + timedelta(days=8)

    cases: list[tuple[str, str]] = [
        (revoked_token, "invitation_revoked"),
        (expiring_token, "invitation_expired"),
    ]
    for token, code in cases:
        with pytest.raises(DomainError) as exc:
            await service.accept_invitation(db_session, invitee, token)
        assert exc.value.code == code
    assert await service.list_invitations(db_session, access) == []


async def test_unknown_invitation_token(db_session: AsyncSession, service: OrgService) -> None:
    with pytest.raises(NotFoundError) as exc:
        await service.preview_invitation(db_session, "not-a-token")
    assert exc.value.code == "invitation_not_found"


async def test_accepting_when_already_a_member_keeps_the_role(
    db_session: AsyncSession, service: OrgService
) -> None:
    access, users = await _org_with(db_session, service, {"admin": Role.ADMIN})
    admin = users["admin"]
    _, token = await service.invite(db_session, access, role=Role.VIEWER, email=admin.email)
    joined = await service.accept_invitation(db_session, admin, token)
    assert joined.role is Role.ADMIN


async def test_invitations_from_another_org_cannot_be_revoked(
    db_session: AsyncSession, service: OrgService
) -> None:
    first, _ = await _org_with(db_session, service, {})
    second, _ = await _org_with(db_session, service, {})
    await service.access(db_session, first.user, first.org.slug)
    invitation, _ = await service.invite(db_session, first, role=Role.MEMBER, email="x@y.io")
    other = await service.access(db_session, second.user, second.org.slug)
    with pytest.raises(NotFoundError):
        await service.revoke_invitation(db_session, other, invitation.id)


async def test_memberships_are_isolated_by_rls(
    db_session: AsyncSession, service: OrgService
) -> None:
    first, _ = await _org_with(db_session, service, {"member": Role.MEMBER})
    second, _ = await _org_with(db_session, service, {})
    # Scoped to the second org, the first org's memberships are invisible.
    await service.access(db_session, second.user, second.org.slug)
    visible = (
        await db_session.scalars(select(Membership).where(Membership.org_id == first.org.id))
    ).all()
    assert visible == []


async def test_a_lost_slug_race_is_a_conflict_not_a_crash(
    db_session: AsyncSession, service: OrgService, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = await make_user(db_session), await make_user(db_session)
    await service.create(db_session, first, name="One", slug="raced-slug")

    async def looks_free(*_: object) -> bool:  # both requests passed the check at once
        return True

    monkeypatch.setattr(service, "slug_available", looks_free)
    with pytest.raises(ConflictError) as exc:
        await service.create(db_session, second, name="Two", slug="raced-slug")
    assert exc.value.code == "slug_taken"
    # The transaction is still usable after the failed insert.
    assert [o.slug for o, _ in await service.list_for_user(db_session, first)] == ["raced-slug"]


async def test_resubmitting_the_current_slug_is_not_a_conflict(
    db_session: AsyncSession, service: OrgService
) -> None:
    access, _ = await _org_with(db_session, service, {})
    org = await service.update(db_session, access, name=None, slug=f"  {access.org.slug.upper()}")
    assert org.slug == access.org.slug


async def test_accepting_twice_is_idempotent(db_session: AsyncSession, service: OrgService) -> None:
    access, _ = await _org_with(db_session, service, {})
    friend = await make_user(db_session, email="twice@example.com")
    _, token = await service.invite(db_session, access, role=Role.MEMBER, email="twice@example.com")
    joined = await service.accept_invitation(db_session, friend, token)
    assert joined.role is Role.MEMBER
    with pytest.raises(ConflictError) as exc:
        await service.accept_invitation(db_session, friend, token)
    assert exc.value.code == "invitation_accepted"


def test_web_slug_rules_match_the_api() -> None:
    """apps/web checks slugs before asking the API; both must agree exactly."""
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from sensei_core.orgs.service import RESERVED_SLUGS, SLUG_RE  # noqa: PLC0415

    source = (
        Path(__file__).resolve().parents[3] / "apps" / "web" / "src" / "lib" / "orgs" / "slug.ts"
    ).read_text()
    web_pattern = re.search(r"export const SLUG_RE = /(.+)/;", source)
    reserved_block = re.search(r"RESERVED_SLUGS = new Set\(\[(.*?)\]\)", source, re.S)
    assert web_pattern is not None
    assert reserved_block is not None
    assert web_pattern.group(1) == SLUG_RE.pattern
    assert set(re.findall(r'"([a-z]+)"', reserved_block.group(1))) == set(RESERVED_SLUGS)
