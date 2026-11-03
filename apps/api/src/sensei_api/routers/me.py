"""The signed-in user and their sessions."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from sensei_api.auth import (
    CurrentUser,
    OptionalBearer,
    RequiredSession,
    Sessions,
    clear_session_cookies,
)
from sensei_api.deps import DbSession
from sensei_api.errors import NotFound
from sensei_api.orgs import OrgServiceDep
from sensei_core.errors import InvalidError
from sensei_core.models import Role, User
from sensei_core.orgs import lifecycle
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


async def _me(
    user: User, db: DbSession, orgs: OrgService, only: uuid.UUID | None = None
) -> MeResponse:
    """The user and their orgs; through an API key or token, only the org it is for (another
    looks like none)."""
    memberships = await orgs.list_for_user(db, user)
    if only is not None:
        memberships = [(org, role) for org, role in memberships if org.id == only]
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
async def get_me(
    user: CurrentUser, bearer: OptionalBearer, db: DbSession, orgs: OrgServiceDep
) -> MeResponse:
    return await _me(user, db, orgs, only=bearer.org_id if bearer is not None else None)


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


class AccountDeletion(BaseModel):
    confirm: str = Field(description="The account's GitHub login, typed again to confirm.")


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    *, request: Request, body: AccountDeletion, active: RequiredSession, db: DbSession
) -> Response:
    """Delete the signed-in account (F19), from a browser session only: 422
    ``confirmation_mismatch`` unless ``confirm`` is the login; 409 ``sole_owner`` while it is an
    org's only owner. Its sessions end, it leaves every org and its name, login, email and
    avatar are gone; what it wrote in orgs stays, by no one."""
    if body.confirm.strip().lower() != active.user.login.lower():
        raise InvalidError("Type your GitHub login to confirm.", code="confirmation_mismatch")
    await lifecycle.delete_account(db, active.user)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, request.app.state.settings)
    return response
