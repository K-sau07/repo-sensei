"""GitHub App user authorization (OAuth 2.0 web flow with PKCE).

Flow: ``start()`` creates a random ``state`` and PKCE verifier (kept in Redis for 10 minutes) and
returns the GitHub authorize URL; the caller also stores ``state`` in a cookie on the browser that
started sign-in. GitHub redirects back with ``code`` + ``state``; ``finish()`` checks the state
against that cookie (so a callback link can't sign someone else's browser into the attacker's
account), consumes the state exactly once and exchanges the code for user tokens. GitHub App user
tokens expire (about 8 hours) and come with a refresh token, see ``refresh()``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from redis.asyncio import Redis

from sensei_core.config import GitHubSettings

STATE_TTL_SECONDS = 600
_STATE_KEY = "oauth:github:state:{}"


class OAuthError(Exception):
    """The OAuth flow failed; ``code`` is safe to show to users and put in URLs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PendingLogin:
    next_path: str


@dataclass(frozen=True, slots=True)
class GitHubTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    refresh_expires_at: datetime | None
    scopes: list[str]


@dataclass(frozen=True, slots=True)
class GitHubUser:
    id: int
    login: str
    name: str | None
    email: str | None
    avatar_url: str | None


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _expiry(seconds: object, now: datetime) -> datetime | None:
    if seconds is None:
        return None
    try:
        return now + timedelta(seconds=int(str(seconds)))
    except ValueError:
        raise OAuthError("token_exchange_failed", "GitHub sent an unexpected response.") from None


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    """The response body if it is a JSON object, else None (GitHub sometimes answers with HTML)."""
    try:
        body: object = response.json()
    except ValueError:
        return None
    return cast("dict[str, Any]", body) if isinstance(body, dict) else None


def _json_objects(response: httpx.Response) -> list[dict[str, Any]]:
    """The JSON objects in a list response; anything else becomes an empty list."""
    try:
        body: object = response.json()
    except ValueError:
        return []
    items = cast("list[object]", body) if isinstance(body, list) else []
    return [cast("dict[str, Any]", item) for item in items if isinstance(item, dict)]


class GitHubOAuth:
    def __init__(
        self, settings: GitHubSettings, redis: Redis, http: httpx.AsyncClient, redirect_uri: str
    ) -> None:
        if not settings.oauth_configured:
            raise OAuthError("github_not_configured", "GitHub sign-in is not configured.")
        self._settings = settings
        self._redis = redis
        self._http = http
        self._redirect_uri = redirect_uri

    async def start(self, next_path: str) -> tuple[str, str]:
        """Remember the login attempt; returns the GitHub authorize URL and the ``state``, which the
        caller must bind to the browser (see ``finish``)."""
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        payload = json.dumps({"verifier": verifier, "next": next_path})
        await self._redis.set(_STATE_KEY.format(state), payload, ex=STATE_TTL_SECONDS)
        query = urlencode(
            {
                "client_id": self._settings.client_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "allow_signup": "true",
            }
        )
        return f"{self._settings.web_url}/login/oauth/authorize?{query}", state

    async def finish(
        self, code: str, state: str, browser_state: str | None
    ) -> tuple[PendingLogin, GitHubTokens]:
        """Check that ``state`` belongs to this browser, consume it (single use) and exchange
        ``code`` for tokens."""
        if not browser_state or not hmac.compare_digest(state, browser_state):
            raise OAuthError("state_mismatch", "This sign-in was started in another browser.")
        raw = await self._redis.getdel(_STATE_KEY.format(state))
        if raw is None:
            raise OAuthError("invalid_state", "The sign-in link expired or was already used.")
        pending = json.loads(raw)
        tokens = await self._token_request(
            {
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": pending["verifier"],
            }
        )
        return PendingLogin(next_path=pending["next"]), tokens

    async def refresh(self, refresh_token: str) -> GitHubTokens:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def fetch_user(self, access_token: str) -> GitHubUser:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = await self._http.get(f"{self._settings.api_url}/user", headers=headers)
        if response.status_code != 200:
            raise OAuthError("github_user_failed", "Could not read your GitHub profile.")
        profile = _json_object(response)
        if profile is None or "id" not in profile or "login" not in profile:
            raise OAuthError("github_user_failed", "Could not read your GitHub profile.")
        email = profile.get("email")
        emails = await self._http.get(f"{self._settings.api_url}/user/emails", headers=headers)
        entries = _json_objects(emails) if emails.status_code == 200 else []
        primary = next((e for e in entries if e.get("primary") and e.get("verified")), None)
        email = primary["email"] if primary else email
        return GitHubUser(
            id=int(profile["id"]),
            login=str(profile["login"]),
            name=profile.get("name"),
            email=email,
            avatar_url=profile.get("avatar_url"),
        )

    async def _token_request(self, params: dict[str, str]) -> GitHubTokens:
        secret = self._settings.client_secret
        if secret is None:  # unreachable: the constructor requires a configured client
            raise OAuthError("github_not_configured", "GitHub sign-in is not configured.")
        data = {
            "client_id": self._settings.client_id or "",
            "client_secret": secret.get_secret_value(),
            **params,
        }
        response = await self._http.post(
            f"{self._settings.web_url}/login/oauth/access_token",
            data=data,
            headers={"Accept": "application/json"},
        )
        body = (_json_object(response) if response.status_code == 200 else None) or {}
        if "access_token" not in body:
            error = str(body.get("error") or "token_exchange_failed")
            raise OAuthError(error, "GitHub did not accept the sign-in. Please try again.")
        now = datetime.now(UTC)
        return GitHubTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=_expiry(body.get("expires_in"), now),
            refresh_expires_at=_expiry(body.get("refresh_token_expires_in"), now),
            scopes=[s for s in str(body.get("scope", "")).split(",") if s],
        )
