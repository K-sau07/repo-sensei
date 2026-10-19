"""Acting as the GitHub App: app JWTs and short-lived installation tokens.

* The **app JWT** (RS256, signed with the app's private key) identifies the app itself. GitHub
  allows at most 10 minutes; we issue 9 and backdate ``iat`` by 60 seconds for clock drift.
* An **installation token** acts on one installation's repositories and expires after an hour.
  Tokens are cached in Redis until 5 minutes before they expire, encrypted (AES-GCM, the Redis key
  bound as associated data), so a Redis dump does not leak repository access.
* Tokens can be narrowed to some repositories and permissions (least privilege); each narrowing is
  cached separately.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
from redis.asyncio import Redis

from sensei_core.config import GitHubSettings
from sensei_core.github.client import Credential, GitHubClient, GitHubError, Now, as_object
from sensei_core.security.crypto import DecryptionError, TokenCipher

JWT_LIFETIME = timedelta(minutes=9)
JWT_BACKDATE = timedelta(seconds=60)
# Reuse a JWT until it has this little life left.
JWT_REUSE_MARGIN = timedelta(minutes=1)
TOKEN_CACHE_MARGIN = timedelta(minutes=5)
MAX_REPOSITORIES = 500  # GitHub's limit for a scoped installation token

_CACHE_KEY = "github:itoken:{installation_id}:{scope}"
_CACHE_INDEX = "github:itoken:{installation_id}:keys"


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def forget_installation_tokens(redis: Redis, installation_id: int) -> None:
    """Drop every cached token for an installation (suspended, uninstalled, permissions
    changed), so the next caller mints one that reflects the change."""
    index = _CACHE_INDEX.format(installation_id=installation_id)
    keys = await cast("Awaitable[set[str]]", redis.smembers(index))  # pyright: ignore[reportUnknownMemberType]
    await redis.delete(index, *keys)


class GitHubAppNotConfiguredError(RuntimeError):
    """The deployment has no GitHub App (slug, client or app id, and private key)."""


@dataclass(frozen=True, slots=True)
class InstallationToken:
    token: str
    expires_at: datetime
    permissions: dict[str, str]
    repository_selection: str | None

    def credential(self, installation_id: int, scope: str) -> Credential:
        return Credential(f"Bearer {self.token}", f"installation:{installation_id}:{scope}")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _scope(repository_ids: Sequence[int] | None, permissions: dict[str, str] | None) -> str:
    """A short, stable name for a token narrowing ("all" for an unrestricted token)."""
    if not repository_ids and not permissions:
        return "all"
    raw = json.dumps([sorted(repository_ids or []), sorted((permissions or {}).items())])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class GitHubApp:
    def __init__(
        self,
        settings: GitHubSettings,
        client: GitHubClient,
        redis: Redis,
        cipher: TokenCipher,
        *,
        now: Now = _utcnow,
    ) -> None:
        if not settings.app_configured:
            msg = "the GitHub App is not configured"
            raise GitHubAppNotConfiguredError(msg)
        self._settings = settings
        self._client = client
        self._redis = redis
        self._cipher = cipher
        self._now = now
        self._issuer = settings.client_id or str(settings.app_id)
        self._private_key = settings.private_key_pem()
        self._jwt: tuple[str, datetime] | None = None

    @property
    def client(self) -> GitHubClient:
        return self._client

    # ------------------------------------------------------------------ app identity

    def app_jwt(self) -> str:
        now = self._now()
        if self._jwt is not None and self._jwt[1] - JWT_REUSE_MARGIN > now:
            return self._jwt[0]
        expires = now + JWT_LIFETIME
        claims = {
            "iat": int((now - JWT_BACKDATE).timestamp()),
            "exp": int(expires.timestamp()),
            "iss": self._issuer,
        }
        token = jwt.encode(claims, self._private_key, algorithm="RS256")
        self._jwt = (token, expires)
        return token

    def app_credential(self) -> Credential:
        return Credential(f"Bearer {self.app_jwt()}", "app")

    async def get_installation(self, installation_id: int) -> dict[str, Any]:
        """The installation as GitHub describes it (account, permissions, events, selection)."""
        body = as_object(
            await self._client.get_json(
                f"/app/installations/{installation_id}", self.app_credential()
            )
        )
        if body is None or body.get("id") != installation_id:
            raise GitHubError(502, "unexpected installation response")
        return body

    # ------------------------------------------------------------------ installation tokens

    async def installation_token(
        self,
        installation_id: int,
        *,
        repository_ids: Sequence[int] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> InstallationToken:
        """A token for ``installation_id``, from the cache when it has 5+ minutes left."""
        if repository_ids is not None and len(repository_ids) > MAX_REPOSITORIES:
            msg = f"a token can be scoped to at most {MAX_REPOSITORIES} repositories"
            raise ValueError(msg)
        scope = _scope(repository_ids, permissions)
        key = _CACHE_KEY.format(installation_id=installation_id, scope=scope)
        cached = await self._read_cached(key)
        if cached is not None:
            return cached

        body: dict[str, Any] = {}
        if repository_ids:
            body["repository_ids"] = list(repository_ids)
        if permissions:
            body["permissions"] = permissions
        response = await self._client.request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            self.app_credential(),
            json_body=body or None,
        )
        data = as_object(response.json())
        if data is None or "token" not in data or "expires_at" not in data:
            raise GitHubError(502, "unexpected access token response")
        token = InstallationToken(
            token=str(data["token"]),
            expires_at=_parse_time(str(data["expires_at"])),
            permissions=dict(data.get("permissions") or {}),
            repository_selection=data.get("repository_selection"),
        )
        await self._write_cached(installation_id, key, token)
        return token

    async def installation_credential(
        self,
        installation_id: int,
        *,
        repository_ids: Sequence[int] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> Credential:
        token = await self.installation_token(
            installation_id, repository_ids=repository_ids, permissions=permissions
        )
        return token.credential(installation_id, _scope(repository_ids, permissions))

    async def forget_tokens(self, installation_id: int) -> None:
        await forget_installation_tokens(self._redis, installation_id)

    async def _read_cached(self, key: str) -> InstallationToken | None:
        raw = cast("str | None", await self._redis.get(key))
        if raw is None:
            return None
        try:
            plain = self._cipher.decrypt_text(base64.b64decode(raw), context=key)
            data = json.loads(plain)
            token = InstallationToken(
                token=data["token"],
                expires_at=_parse_time(data["expires_at"]),
                permissions=data["permissions"],
                repository_selection=data["repository_selection"],
            )
        except (DecryptionError, ValueError, KeyError, TypeError):
            await self._redis.delete(key)  # unreadable (e.g. key rotated away): mint a new one
            return None
        if token.expires_at - TOKEN_CACHE_MARGIN <= self._now():
            return None
        return token

    async def _write_cached(self, installation_id: int, key: str, token: InstallationToken) -> None:
        ttl = int((token.expires_at - TOKEN_CACHE_MARGIN - self._now()).total_seconds())
        if ttl <= 0:
            return
        payload = json.dumps(
            {
                "token": token.token,
                "expires_at": token.expires_at.isoformat(),
                "permissions": token.permissions,
                "repository_selection": token.repository_selection,
            }
        )
        sealed = base64.b64encode(self._cipher.encrypt_text(payload, context=key)).decode()
        index = _CACHE_INDEX.format(installation_id=installation_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(key, sealed, ex=ttl)
            pipe.sadd(index, key)  # pyright: ignore[reportUnknownMemberType]
            pipe.expire(index, 3600)  # tokens live an hour at most
            await pipe.execute()  # pyright: ignore[reportUnknownMemberType]
