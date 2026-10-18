"""Organizations, members and invitations."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from sensei_api.auth import CurrentUser
from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep, OrgServiceDep
from sensei_core.errors import InvalidError
from sensei_core.models import Org, Role
from sensei_core.orgs.service import validate_slug

router = APIRouter(prefix="/orgs", tags=["orgs"])


class OrgOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    avatar_url: str | None
    plan: str
    role: Role

    @classmethod
    def build(cls, org: Org, role: Role) -> OrgOut:
        return cls(
            id=org.id,
            slug=org.slug,
            name=org.name,
            avatar_url=org.avatar_url,
            plan=org.plan,
            role=role,
        )


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=3, max_length=39)


class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    slug: str | None = Field(default=None, min_length=3, max_length=39)


class SlugAvailability(BaseModel):
    slug: str
    available: bool
    reason: str | None = None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    login: str
    name: str | None
    avatar_url: str | None
    role: Role
    joined_at: datetime


class RoleUpdate(BaseModel):
    role: Role


class InvitationCreate(BaseModel):
    email: str | None = Field(default=None, max_length=320)
    github_login: str | None = Field(default=None, max_length=40)
    role: Role = Role.MEMBER


class InvitationOut(BaseModel):
    id: uuid.UUID
    email: str | None
    github_login: str | None
    role: Role
    expires_at: datetime
    created_at: datetime


class InvitationCreated(InvitationOut):
    link: str = Field(description="One-time link; shown only now.")


@router.get("", response_model=list[OrgOut])
async def list_my_orgs(user: CurrentUser, db: DbSession, orgs: OrgServiceDep) -> list[OrgOut]:
    return [OrgOut.build(org, role) for org, role in await orgs.list_for_user(db, user)]


@router.post("", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: OrgCreate, user: CurrentUser, db: DbSession, orgs: OrgServiceDep
) -> OrgOut:
    access = await orgs.create(db, user, name=body.name, slug=body.slug)
    return OrgOut.build(access.org, access.role)


@router.get("/slug-available", response_model=SlugAvailability)
async def slug_available(
    _user: CurrentUser, db: DbSession, orgs: OrgServiceDep, slug: str = Query(max_length=64)
) -> SlugAvailability:
    try:
        normalized = validate_slug(slug)
    except InvalidError as exc:
        return SlugAvailability(slug=slug, available=False, reason=exc.code)
    available = await orgs.slug_available(db, normalized)
    return SlugAvailability(
        slug=normalized, available=available, reason=None if available else "slug_taken"
    )


@router.get("/{org}", response_model=OrgOut)
async def get_org(access: OrgAccessDep) -> OrgOut:
    return OrgOut.build(access.org, access.role)


@router.patch("/{org}", response_model=OrgOut)
async def update_org(
    body: OrgUpdate, access: OrgAccessDep, db: DbSession, orgs: OrgServiceDep
) -> OrgOut:
    org = await orgs.update(db, access, name=body.name, slug=body.slug)
    return OrgOut.build(org, access.role)


@router.delete("/{org}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org(access: OrgAccessDep, db: DbSession, orgs: OrgServiceDep) -> Response:
    await orgs.delete(db, access)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{org}/members", response_model=list[MemberOut])
async def list_members(access: OrgAccessDep, db: DbSession, orgs: OrgServiceDep) -> list[MemberOut]:
    return [
        MemberOut(
            user_id=user.id,
            login=user.login,
            name=user.name,
            avatar_url=user.avatar_url,
            role=membership.role,
            joined_at=membership.created_at,
        )
        for user, membership in await orgs.list_members(db, access)
    ]


@router.patch("/{org}/members/{user_id}", response_model=MemberOut)
async def change_member_role(
    user_id: uuid.UUID,
    body: RoleUpdate,
    access: OrgAccessDep,
    db: DbSession,
    orgs: OrgServiceDep,
) -> MemberOut:
    membership = await orgs.change_role(db, access, user_id, body.role)
    member = next(u for u, m in await orgs.list_members(db, access) if m.user_id == user_id)
    return MemberOut(
        user_id=member.id,
        login=member.login,
        name=member.name,
        avatar_url=member.avatar_url,
        role=membership.role,
        joined_at=membership.created_at,
    )


@router.delete("/{org}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID, access: OrgAccessDep, db: DbSession, orgs: OrgServiceDep
) -> Response:
    await orgs.remove_member(db, access, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{org}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_org(access: OrgAccessDep, db: DbSession, orgs: OrgServiceDep) -> Response:
    await orgs.leave(db, access)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{org}/invitations", response_model=list[InvitationOut])
async def list_invitations(
    access: OrgAccessDep, db: DbSession, orgs: OrgServiceDep
) -> list[InvitationOut]:
    return [
        InvitationOut(
            id=i.id,
            email=i.email,
            github_login=i.github_login,
            role=i.role,
            expires_at=i.expires_at,
            created_at=i.created_at,
        )
        for i in await orgs.list_invitations(db, access)
    ]


@router.post(
    "/{org}/invitations", response_model=InvitationCreated, status_code=status.HTTP_201_CREATED
)
async def create_invitation(
    body: InvitationCreate, access: OrgAccessDep, db: DbSession, orgs: OrgServiceDep
) -> InvitationCreated:
    invitation, token = await orgs.invite(
        db, access, role=body.role, email=body.email, github_login=body.github_login
    )
    origin: str = orgs.web_origin
    return InvitationCreated(
        id=invitation.id,
        email=invitation.email,
        github_login=invitation.github_login,
        role=invitation.role,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
        link=f"{origin}/invite/{token}",
    )


@router.delete("/{org}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: uuid.UUID, access: OrgAccessDep, db: DbSession, orgs: OrgServiceDep
) -> Response:
    await orgs.revoke_invitation(db, access, invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
