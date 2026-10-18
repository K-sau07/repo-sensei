"""Invitation links: preview before signing in, accept after."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sensei_api.auth import CurrentUser
from sensei_api.deps import DbSession
from sensei_api.orgs import OrgServiceDep
from sensei_api.routers.orgs import OrgOut
from sensei_core.models import Role

router = APIRouter(prefix="/invitations", tags=["invitations"])


class InvitationPreviewOut(BaseModel):
    org_name: str
    org_slug: str
    inviter_login: str | None
    role: Role
    status: str


@router.get("/{token}", response_model=InvitationPreviewOut)
async def preview_invitation(
    token: str, db: DbSession, orgs: OrgServiceDep
) -> InvitationPreviewOut:
    preview = await orgs.preview_invitation(db, token)
    return InvitationPreviewOut(
        org_name=preview.org_name,
        org_slug=preview.org_slug,
        inviter_login=preview.inviter_login,
        role=preview.role,
        status=preview.status,
    )


@router.post("/{token}/accept", response_model=OrgOut)
async def accept_invitation(
    token: str, user: CurrentUser, db: DbSession, orgs: OrgServiceDep
) -> OrgOut:
    access = await orgs.accept_invitation(db, user, token)
    return OrgOut.build(access.org, access.role)
