"""Signing out. (Signing in arrives with GitHub OAuth.)"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from sensei_api.auth import RequiredSession, Sessions, clear_session_cookies
from sensei_api.deps import DbSession

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, active: RequiredSession, db: DbSession, sessions: Sessions
) -> Response:
    await sessions.revoke(db, active.user.id, active.session.id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, request.app.state.settings)
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request, active: RequiredSession, db: DbSession, sessions: Sessions
) -> Response:
    await sessions.revoke_all(db, active.user.id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, request.app.state.settings)
    return response
