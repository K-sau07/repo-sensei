"""The signed-in user and their sessions."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from sensei_api.auth import CurrentUser, RequiredSession, Sessions
from sensei_api.deps import DbSession
from sensei_api.errors import NotFound
from sensei_api.orgs import OrgServiceDep
from sensei_core.models import Role, User
from sensei_core.orgs.service import OrgService

router = APIRouter(prefix="/me", tags=["me"])


class MyOrg(BaseModel):
    slug: str
    name: str
    role: Role


class MeResponse(BaseModel):
    id: uuid.UUID
    login: str
    name: str | None
    email: str | None
    avatar_url: str | None
    is_staff: bool
    orgs: list[MyOrg]
    last_org_slug: str | None


class MeUpdate(BaseModel):
    last_org_slug: str | None = None


class SessionInfo(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool


async def _me(user: User, db: DbSession, orgs: OrgService) -> MeResponse:
    memberships = await orgs.list_for_user(db, user)
    slugs = {org.slug for org, _ in memberships}
    return MeResponse(
        id=user.id,
        login=user.login,
        name=user.name,
        email=user.email,
        avatar_url=user.avatar_url,
        is_staff=user.is_staff,
        orgs=[MyOrg(slug=org.slug, name=org.name, role=role) for org, role in memberships],
        last_org_slug=user.last_org_slug if user.last_org_slug in slugs else None,
    )


@router.get("", response_model=MeResponse)
async def get_me(user: CurrentUser, db: DbSession, orgs: OrgServiceDep) -> MeResponse:
    return await _me(user, db, orgs)


@router.patch("", response_model=MeResponse)
async def update_me(
    body: MeUpdate, user: CurrentUser, db: DbSession, orgs: OrgServiceDep
) -> MeResponse:
    if body.last_org_slug is not None:
        access = await orgs.access(db, user, body.last_org_slug)  # must be a member
        user.last_org_slug = access.org.slug
        await db.flush()
    return await _me(user, db, orgs)


@router.get("/sessions", response_model=list[SessionInfo])
async def list_sessions(
    active: RequiredSession, db: DbSession, sessions: Sessions
) -> list[SessionInfo]:
    return [
        SessionInfo(
            id=s.id,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            ip=str(s.ip) if s.ip else None,
            user_agent=s.user_agent,
            current=s.id == active.session.id,
        )
        for s in await sessions.list_active(db, active.user.id)
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID, active: RequiredSession, db: DbSession, sessions: Sessions
) -> Response:
    if not await sessions.revoke(db, active.user.id, session_id):
        raise NotFound("Session not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
