"""Cookie sessions, API keys, CSRF protection and the authentication dependencies.

Cookies (``__Host-`` prefixed when secure: no Domain, Path=/, Secure):
  * session - HttpOnly, SameSite=Lax: the session token.
  * csrf    - readable by the web app, SameSite=Lax: echoed back in ``X-CSRF-Token``.

For unsafe methods (POST/PUT/PATCH/DELETE) on a cookie-authenticated request we require:
  1. ``Origin`` (or ``Referer``) to be the web app's origin, and
  2. ``X-CSRF-Token`` to equal the csrf cookie *and* to match the hash stored on the session,
     so a token cannot be replayed across sessions.

An API key (7.1) comes as ``Authorization: Bearer rs_…``, an OAuth access token (7.2) as
``Bearer rsat_…`` (for the API resource only); then cookies are ignored: no CSRF (a browser never
sends it by itself). It acts as its user, only in its own org
(:func:`sensei_api.orgs.get_org_access`) and only where a router lets keys in, within the key's
scopes (:func:`key_access`; routers not let in refuse keys, and so does anything without a
rule).
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Collection
from typing import Annotated
from urllib.parse import urlsplit

import structlog
from fastapi import Depends, Request, Response

from sensei_api.deps import DbSession
from sensei_api.errors import AppError, Forbidden, Unauthorized
from sensei_core.auth.api_keys import ApiKeyService, KeyPrincipal, KeyRejected, Scope
from sensei_core.auth.sessions import ActiveSession, IssuedSession, SessionService
from sensei_core.config import Settings
from sensei_core.errors import DomainError
from sensei_core.models import ActorType, User
from sensei_core.oauth.service import ACCESS_PREFIX, OAuthService, TokenPrincipal, TokenRejected

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-csrf-token"
KEY_PREFIX = "rs_"
REJECTED = {
    "invalid": "This API key isn't valid.",
    "revoked": "This API key was revoked.",
    "expired": "This API key has expired.",
}
TOKEN_REJECTED = {
    "invalid": "This access token isn't valid.",
    "revoked": "This access token was revoked.",
    "expired": "This access token has expired.",
    "audience": "This access token is for another resource.",
}


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


def _bearer_key(request: Request) -> str | None:
    """The API key or OAuth access token the request carries, if it carries one of ours."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith((KEY_PREFIX, ACCESS_PREFIX)):
        return None
    return token


def get_api_key_service(request: Request) -> ApiKeyService:
    service: ApiKeyService = request.app.state.api_key_service
    return service


def get_oauth_service(request: Request) -> OAuthService:
    service: OAuthService = request.app.state.oauth_service
    return service


class TokenUnauthorized(DomainError):
    status_code = 401
    code = "invalid_token"


class TokenForbidden(DomainError):
    status_code = 403
    code = "insufficient_scope"


Principal = KeyPrincipal | TokenPrincipal


def _challenge(oauth: OAuthService, error: str, **extra: str) -> dict[str, str]:
    """RFC 6750's challenge, pointing at the API's protected resource metadata (RFC 9728)."""
    params = {
        "error": error,
        **extra,
        "resource_metadata": f"{oauth.issuer}/.well-known/oauth-protected-resource/api",
    }
    return {"WWW-Authenticate": "Bearer " + ", ".join(f'{k}="{v}"' for k, v in params.items())}


async def get_bearer(
    request: Request,
    db: DbSession,
    keys: Annotated[ApiKeyService, Depends(get_api_key_service)],
    oauth: Annotated[OAuthService, Depends(get_oauth_service)],
) -> Principal | None:
    """Resolve a bearer API key or OAuth access token (once per request): its principal, or
    401 for a bad one. An OAuth token must be for the API (RFC 8707: one for the MCP server is
    refused here)."""
    token = _bearer_key(request)
    if token is None:
        return None
    found: Principal
    if token.startswith(ACCESS_PREFIX):
        try:
            found = await oauth.authenticate(db, token, resource=oauth.resources.api)
        except TokenRejected as rejected:
            raise TokenUnauthorized(
                TOKEN_REJECTED[rejected.reason],
                headers=_challenge(oauth, "invalid_token"),
            ) from None
        actor = {"actor_type": ActorType.OAUTH.value, "oauth_token_id": str(found.id)}
    else:
        try:
            found = await keys.authenticate(db, token)
        except KeyRejected as rejected:
            raise Unauthorized(
                REJECTED[rejected.reason], code=f"api_key_{rejected.reason}"
            ) from None
        actor = {"actor_type": ActorType.API_KEY.value, "api_key_id": str(found.id)}
    # The router's rule (``key_access``) said with which scope bearers come in; none: not at all.
    scope: Scope | None = getattr(request.state, "key_scope", None)
    if scope is None:
        raise Forbidden("API keys can't be used for this.", code="api_key_not_allowed")
    if not found.allows(scope):
        if isinstance(found, TokenPrincipal):
            raise TokenForbidden(
                f"This token doesn't have the {scope.value} scope.",
                details={"required_scope": scope.value},
                headers=_challenge(oauth, "insufficient_scope", scope=scope.value),
            )
        raise Forbidden(
            f"This API key doesn't have the {scope.value} scope.",
            code="api_key_scope",
            details={"required_scope": scope.value},
        )
    request.state.principal = found
    request.state.user_id = found.user.id
    structlog.contextvars.bind_contextvars(**actor)
    return found


OptionalBearer = Annotated[Principal | None, Depends(get_bearer)]


async def get_active_session(
    request: Request,
    db: DbSession,
    sessions: Sessions,
) -> ActiveSession | None:
    """Resolve the cookie session (once per request) and enforce CSRF on unsafe methods. A
    request carrying an API key has no session (its cookies are ignored)."""
    settings: Settings = request.app.state.settings
    session_cookie, csrf_cookie = cookie_names(settings)
    token = request.cookies.get(session_cookie)
    if not token or _bearer_key(request) is not None:
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


async def require_session(active: OptionalSession, key: OptionalBearer) -> ActiveSession:
    """A browser session: what an API key can't stand in for (sessions, signing out...)."""
    if active is None:
        if key is not None:
            raise Forbidden("API keys can't be used for this.", code="api_key_not_allowed")
        raise Unauthorized("Sign in to continue.")
    return active


RequiredSession = Annotated[ActiveSession, Depends(require_session)]


async def require_user(active: OptionalSession, key: OptionalBearer) -> User:
    """The signed-in user: a browser's, or the one an API key acts as."""
    if key is not None:
        return key.user
    if active is None:
        raise Unauthorized("Sign in to continue.")
    return active.user


CurrentUser = Annotated[User, Depends(require_user)]


async def require_staff(user: CurrentUser) -> User:
    if not user.is_staff:
        raise AppError("Not found.", code="not_found", status_code=404)
    return user


CurrentStaff = Annotated[User, Depends(require_staff)]


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def refuse_keys(request: Request) -> None:
    """For routers keys aren't let in: refused on sight, unchecked (no database needed, so
    health checks and webhooks stay as they are)."""
    if _bearer_key(request) is not None:
        raise Forbidden("API keys can't be used for this.", code="api_key_not_allowed")


def key_access(
    scope: Scope | None, writes: Collection[Callable[..., object]] = ()
) -> Callable[..., Awaitable[None]]:
    """A router's rule for API keys: refused (``scope`` None), or let in with ``scope`` to read,
    and to call the unsafe endpoints in ``writes`` (the handler functions). Checked from the
    header alone (no database: public routes stay public); the key and its scope are checked
    where an endpoint needs its user (:func:`get_bearer`). Requests without a key pass."""
    if scope is None:
        return refuse_keys

    async def rule(request: Request) -> None:
        if _bearer_key(request) is None:
            return
        request.state.key_scope = scope
        endpoint = request.scope.get("endpoint")
        if request.method not in SAFE_METHODS and endpoint not in writes:
            raise Forbidden("API keys can't be used for this.", code="api_key_not_allowed")

    return rule
