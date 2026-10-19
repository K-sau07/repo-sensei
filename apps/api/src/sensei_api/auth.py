"""Cookie sessions, CSRF protection and the authentication dependencies.

Cookies (``__Host-`` prefixed when secure: no Domain, Path=/, Secure):
  * session - HttpOnly, SameSite=Lax: the session token.
  * csrf    - readable by the web app, SameSite=Lax: echoed back in ``X-CSRF-Token``.

For unsafe methods (POST/PUT/PATCH/DELETE) on a cookie-authenticated request we require:
  1. ``Origin`` (or ``Referer``) to be the web app's origin, and
  2. ``X-CSRF-Token`` to equal the csrf cookie *and* to match the hash stored on the session,
     so a token cannot be replayed across sessions.
"""

from __future__ import annotations

import hmac
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request, Response

from sensei_api.deps import DbSession
from sensei_api.errors import AppError, Forbidden, Unauthorized
from sensei_core.auth.sessions import ActiveSession, IssuedSession, SessionService
from sensei_core.config import Settings
from sensei_core.models import User

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-csrf-token"


def cookie_names(settings: Settings) -> tuple[str, str]:
    if settings.secure_cookies:
        return "__Host-session", "__Host-csrf"
    return "session", "csrf"


def oauth_state_cookie_name(settings: Settings) -> str:
    """Binds a GitHub sign-in to the browser that started it (see GitHubOAuth.finish)."""
    return "__Host-oauth_state" if settings.secure_cookies else "oauth_state"


def set_session_cookies(response: Response, issued: IssuedSession, settings: Settings) -> None:
    session_cookie, csrf_cookie = cookie_names(settings)
    max_age = settings.auth.session_absolute_days * 86400
    common = {"max_age": max_age, "path": "/", "secure": settings.secure_cookies}
    response.set_cookie(session_cookie, issued.token, httponly=True, samesite="lax", **common)  # pyright: ignore[reportArgumentType]
    response.set_cookie(csrf_cookie, issued.csrf_token, httponly=False, samesite="lax", **common)  # pyright: ignore[reportArgumentType]


def clear_session_cookies(response: Response, settings: Settings) -> None:
    session_cookie, csrf_cookie = cookie_names(settings)
    for name in (session_cookie, csrf_cookie):
        response.delete_cookie(name, path="/", secure=settings.secure_cookies, samesite="lax")


def _origin_of(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}"
    return None


def get_session_service(request: Request) -> SessionService:
    service: SessionService = request.app.state.session_service
    return service


Sessions = Annotated[SessionService, Depends(get_session_service)]


async def get_active_session(
    request: Request,
    db: DbSession,
    sessions: Sessions,
) -> ActiveSession | None:
    """Resolve the cookie session (once per request) and enforce CSRF on unsafe methods."""
    settings: Settings = request.app.state.settings
    session_cookie, csrf_cookie = cookie_names(settings)
    token = request.cookies.get(session_cookie)
    if not token:
        return None
    active = await sessions.authenticate(db, token)
    if active is None:
        raise Unauthorized(
            "Your session has expired. Please sign in again.", code="session_expired"
        )

    if request.method in UNSAFE_METHODS:
        if _origin_of(request) != settings.web.origin:
            raise Forbidden("Request origin is not allowed.", code="csrf_failed")
        header = request.headers.get(CSRF_HEADER, "")
        cookie = request.cookies.get(csrf_cookie, "")
        if not (
            header
            and hmac.compare_digest(header, cookie)
            and sessions.csrf_matches(active.session, header)
        ):
            raise Forbidden("Missing or invalid CSRF token.", code="csrf_failed")

    request.state.active_session = active
    request.state.user_id = active.user.id
    return active


OptionalSession = Annotated[ActiveSession | None, Depends(get_active_session)]


async def require_session(active: OptionalSession) -> ActiveSession:
    if active is None:
        raise Unauthorized("Sign in to continue.")
    return active


RequiredSession = Annotated[ActiveSession, Depends(require_session)]


async def require_user(active: RequiredSession) -> User:
    return active.user


CurrentUser = Annotated[User, Depends(require_user)]


async def require_staff(user: CurrentUser) -> User:
    if not user.is_staff:
        raise AppError("Not found.", code="not_found", status_code=404)
    return user


CurrentStaff = Annotated[User, Depends(require_staff)]
