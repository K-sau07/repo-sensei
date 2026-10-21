"""Sign in with GitHub, and sign out."""

from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis

from sensei_api.auth import (
    RequiredSession,
    Sessions,
    clear_session_cookies,
    oauth_state_cookie_name,
    set_session_cookies,
)
from sensei_api.deps import DbSession
from sensei_api.errors import AppError
from sensei_core.audit import AuditAction, audit
from sensei_core.auth.github_login import complete_login
from sensei_core.config import Settings
from sensei_core.context import current_client_ip
from sensei_core.github.oauth import STATE_TTL_SECONDS, GitHubOAuth, OAuthError
from sensei_core.logging import get_logger
from sensei_core.security.crypto import TokenCipher

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)

CALLBACK_PATH = "/api/v1/auth/github/callback"
_SAFE_NEXT = re.compile(r"^/(?![/\\])[\x21-\x7e]{0,511}$")


def safe_next(value: str | None) -> str:
    """Only same-site relative paths are allowed after sign-in (no open redirects)."""
    if value and _SAFE_NEXT.match(value) and "\\" not in value:
        return value
    return "/"


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_github_oauth(request: Request) -> GitHubOAuth | None:
    """None when this deployment has no GitHub App yet. These endpoints are browser navigations,
    so callers send the user back to the login page with an explanation instead of an error."""
    settings = _settings(request)
    if not settings.github.oauth_configured:
        return None
    redis: Redis = request.app.state.redis
    http: httpx.AsyncClient = request.app.state.http
    try:
        return GitHubOAuth(
            settings.github, redis, http, redirect_uri=f"{settings.web.origin}{CALLBACK_PATH}"
        )
    except OAuthError as exc:
        raise AppError(str(exc), code=exc.code, status_code=503) from exc


GitHubOAuthDep = Annotated[GitHubOAuth | None, Depends(get_github_oauth)]
NOT_CONFIGURED = "/login?error=github_not_configured"


def _web_redirect(request: Request, path: str) -> RedirectResponse:
    response = RedirectResponse(f"{_settings(request).web.origin}{path}", status_code=302)
    # The sign-in attempt is over either way; its state cookie must not outlive it.
    settings = _settings(request)
    response.delete_cookie(
        oauth_state_cookie_name(settings), path="/", secure=settings.secure_cookies, samesite="lax"
    )
    return response


@router.get("/github/login", response_class=RedirectResponse, status_code=302)
async def github_login(
    request: Request,
    oauth: GitHubOAuthDep,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    """Send the browser to GitHub to authorize repo-sensei."""
    if oauth is None:
        return _web_redirect(request, NOT_CONFIGURED)
    url, state = await oauth.start(safe_next(next_path))
    response = RedirectResponse(url, status_code=302)
    settings = _settings(request)
    # Lax (not Strict): GitHub's redirect back to the callback is a cross-site top-level GET.
    response.set_cookie(
        oauth_state_cookie_name(settings),
        state,
        max_age=STATE_TTL_SECONDS,
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/github/callback", response_class=RedirectResponse, status_code=302)
async def github_callback(
    *,
    request: Request,
    oauth: GitHubOAuthDep,
    db: DbSession,
    sessions: Sessions,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """GitHub redirects here; on success the browser gets a session and goes to the web app."""
    if oauth is None:
        return _web_redirect(request, NOT_CONFIGURED)
    if error or not code or not state:
        reason = "access_denied" if error == "access_denied" else "oauth_failed"
        return _web_redirect(request, f"/login?error={reason}")
    try:
        browser_state = request.cookies.get(oauth_state_cookie_name(_settings(request)))
        pending, tokens = await oauth.finish(code, state, browser_state)
        cipher: TokenCipher = request.app.state.token_cipher
        user = await complete_login(db, cipher, oauth, tokens)
    except OAuthError as exc:
        log.warning("github sign-in failed", reason=exc.code)
        return _web_redirect(request, f"/login?error={quote(exc.code)}")

    issued = await sessions.create(
        db,
        user,
        ip=current_client_ip(),
        user_agent=request.headers.get("user-agent"),
    )
    await audit(
        db,
        AuditAction.LOGIN,
        actor_user_id=user.id,
        target_type="session",
        target_id=issued.session.id,
        meta={"method": "github"},
    )
    log.info("user signed in", user_id=str(user.id), github_login=user.login)
    response = _web_redirect(request, f"/auth/callback?next={quote(pending.next_path, safe='/')}")
    set_session_cookies(response, issued, _settings(request))
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, active: RequiredSession, db: DbSession, sessions: Sessions
) -> Response:
    await sessions.revoke(db, active.user.id, active.session.id)
    await audit(
        db,
        AuditAction.LOGOUT,
        actor_user_id=active.user.id,
        target_type="session",
        target_id=active.session.id,
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, _settings(request))
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request, active: RequiredSession, db: DbSession, sessions: Sessions
) -> Response:
    revoked = await sessions.revoke_all(db, active.user.id)
    await audit(
        db,
        AuditAction.LOGOUT_ALL,
        actor_user_id=active.user.id,
        target_type="user",
        target_id=active.user.id,
        meta={"sessions_revoked": revoked},
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, _settings(request))
    return response
