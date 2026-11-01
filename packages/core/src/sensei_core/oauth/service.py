"""The OAuth 2.1 authorization server (7.2), as the MCP authorization spec (2026-07-28) asks.

* **Authorization code + PKCE** (S256 only): the client's request is checked (client, exact
  redirect URI, scopes, ``resource``), kept for the user to decide on the consent page, then
  answered at the redirect URI with a one-use code, ``state`` and ``iss`` (RFC 9207). The code,
  with its verifier, buys tokens; a code used twice revokes what it bought.
* **Device flow** (RFC 8628, for the CLI): a device code to poll with and a short user code to
  type at ``/device``; polling faster than the interval is told to ``slow_down``.
* **Tokens**: opaque, kept as SHA-256 hashes; an access token lasts an hour, a refresh token a
  month and is used once (each use issues the next of its family; using one twice revokes the
  family). Each token is for one org (chosen on the consent page) and one **resource** (RFC
  8707): the MCP server or the API, and is refused anywhere else.

Errors are :class:`OAuthError`: the RFC 6749 error code and a description.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.auth.api_keys import Scope
from sensei_core.config import Settings
from sensei_core.db.base import new_id
from sensei_core.db.tenancy import set_org_context
from sensei_core.models import (
    GrantStatus,
    IssuedToken,
    OAuthClient,
    OAuthDeviceCode,
    OAuthGrant,
    TokenKind,
    User,
)
from sensei_core.oauth.clients import (
    GRANT_CODE,
    GRANT_DEVICE,
    GRANT_REFRESH,
    ClientError,
    ClientRegistry,
    grant_types,
    redirect_allowed,
)
from sensei_core.orgs.service import OrgAccess

Clock = Callable[[], datetime]

SCOPES = (Scope.READ_REPO.value, Scope.ASK.value)
DEFAULT_SCOPES = (Scope.READ_REPO.value,)
IGNORED_SCOPES = frozenset({"offline_access"})  # refresh tokens are always given
REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=5)
DEVICE_TTL = timedelta(minutes=10)
DEVICE_INTERVAL = 5
SLOW_DOWN = 5
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"  # no vowels (no words), no look-alikes
_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9\-_]{43}$")
ACCESS_PREFIX = "rsat_"
REFRESH_PREFIX = "rsrt_"
DEVICE_PREFIX = "rsdc_"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _hash(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def _secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


class OAuthError(Exception):
    """An RFC 6749 error. ``redirect``: whether it may go back to the client's redirect URI
    (not when the client or its redirect URI is the problem)."""

    def __init__(
        self, error: str, description: str, *, status: int = 400, redirect: bool = True
    ) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status
        self.redirect = redirect


@dataclass(frozen=True, slots=True)
class Resources:
    """What tokens can be for: the MCP server and the API, under the issuer."""

    issuer: str

    @property
    def mcp(self) -> str:
        return f"{self.issuer}/mcp"

    @property
    def api(self) -> str:
        return f"{self.issuer}/api"

    def canonical(self, resource: str | None, *, default: str) -> str:
        """``resource`` as one of ours (scheme and host case, a trailing slash forgiven), or
        ``invalid_target``."""
        if resource is None or not resource.strip():
            return default
        parts = urlsplit(resource.strip())
        if parts.fragment or parts.query:
            raise OAuthError("invalid_target", "The resource has a query or fragment.")
        path = parts.path.rstrip("/")
        normal = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
        for ours in (self.mcp, self.api):
            if normal == ours.lower() or normal == ours:
                return ours
        raise OAuthError("invalid_target", "Tokens can be had for this MCP server or API only.")


@dataclass(frozen=True, slots=True)
class Issued:
    access_token: str
    refresh_token: str
    expires_in: int
    scopes: list[str]

    def as_json(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": " ".join(self.scopes),
        }


@dataclass(frozen=True, slots=True)
class Pending:
    """An authorization waiting for the user: what the consent page shows."""

    grant: OAuthGrant
    client: OAuthClient


@dataclass(frozen=True, slots=True)
class TokenPrincipal:
    """A checked access token: who it acts as, in which org, with which scopes."""

    token: IssuedToken
    user: User

    @property
    def org_id(self) -> uuid.UUID:
        return self.token.org_id

    @property
    def id(self) -> uuid.UUID:
        return self.token.id

    def allows(self, scope: Scope) -> bool:
        return scope.value in self.token.scopes


class TokenRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # invalid | expired | revoked | audience


def parse_scopes(raw: str | None) -> list[str]:
    """The scopes asked for (space-separated), ours only; none asked: :data:`DEFAULT_SCOPES`."""
    asked = [s for s in (raw or "").split() if s not in IGNORED_SCOPES]
    if not asked:
        return list(DEFAULT_SCOPES)
    unknown = [s for s in asked if s not in SCOPES]
    if unknown:
        raise OAuthError("invalid_scope", f"Unknown scope: {', '.join(unknown)}.")
    return sorted(set(asked))


def pkce_ok(verifier: str, challenge: str) -> bool:
    if not _VERIFIER.match(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def _decided(user_id: uuid.UUID | None, org_id: uuid.UUID | None) -> tuple[uuid.UUID, uuid.UUID]:
    """Who approved, and for which org (set when the user approved)."""
    if user_id is None or org_id is None:
        raise OAuthError("invalid_grant", "Not approved.")
    return user_id, org_id


def new_user_code() -> str:
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))


def normal_user_code(typed: str) -> str:
    return re.sub(r"[^A-Z]", "", typed.upper())


def show_user_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def with_query(uri: str, params: dict[str, str | None]) -> str:
    parts = urlsplit(uri)
    extra = urlencode({k: v for k, v in params.items() if v is not None})
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


class OAuthService:
    def __init__(
        self, settings: Settings, registry: ClientRegistry, *, clock: Clock = utcnow
    ) -> None:
        self.issuer = (settings.oauth.issuer or settings.web.origin).rstrip("/")
        self.resources = Resources(self.issuer)
        self.registry = registry
        self._clock = clock
        self._access_ttl = timedelta(minutes=settings.oauth.access_token_minutes)
        self._refresh_ttl = timedelta(days=settings.oauth.refresh_token_days)

    # ---------------------------------------------------------------- metadata

    def server_metadata(self) -> dict[str, object]:
        """RFC 8414 (and what MCP clients look for)."""
        base = f"{self.issuer}/api/v1/oauth"
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "revocation_endpoint": f"{base}/revoke",
            "device_authorization_endpoint": f"{base}/device/code",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": [GRANT_CODE, GRANT_REFRESH, GRANT_DEVICE],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SCOPES),
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "service_documentation": f"{self.issuer}/docs/mcp",
        }

    def resource_metadata(self, resource: str) -> dict[str, object]:
        """RFC 9728, for the MCP server or the API."""
        return {
            "resource": resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": "repo-sensei",
        }

    # ---------------------------------------------------------------- authorization code

    async def client(self, db: AsyncSession, client_id: str | None) -> OAuthClient:
        if not client_id:
            raise OAuthError("invalid_request", "client_id is missing.", redirect=False)
        try:
            return await self.registry.get(db, client_id, now=self._clock())
        except ClientError as error:
            raise OAuthError(error.error, error.description, redirect=False) from None

    async def begin(
        self,
        db: AsyncSession,
        *,
        client_id: str | None,
        redirect_uri: str | None,
        response_type: str | None,
        code_challenge: str | None,
        code_challenge_method: str | None,
        scope: str | None,
        state: str | None,
        resource: str | None,
    ) -> tuple[str, Pending]:
        """Check an authorization request and keep it for the user to decide on: its handle
        (for the consent page) and what it asks. :class:`OAuthError` with ``redirect`` False:
        show the error here; True: send it to the redirect URI (:meth:`error_redirect`)."""
        client = await self.client(db, client_id)
        if redirect_uri is None and len(client.redirect_uris) == 1:
            redirect_uri = client.redirect_uris[0]
        if not redirect_uri or not redirect_allowed(client, redirect_uri):
            raise OAuthError(
                "invalid_request", "The redirect_uri isn't one of the client's.", redirect=False
            )
        if GRANT_CODE not in grant_types(client):
            raise OAuthError("unauthorized_client", "This client can't use authorization codes.")
        if response_type != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
        if (
            code_challenge_method != "S256"
            or not code_challenge
            or not _CHALLENGE.match(code_challenge)
        ):
            raise OAuthError("invalid_request", "PKCE with S256 is required.")
        scopes = parse_scopes(scope)
        target = self.resources.canonical(resource, default=self.resources.mcp)
        handle = secrets.token_urlsafe(32)
        grant = OAuthGrant(
            id=new_id(),
            request_hash=_hash(handle),
            client_id=client.id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            resource=target,
            code_challenge=code_challenge,
            state=state,
            status=GrantStatus.PENDING,
            expires_at=self._clock() + REQUEST_TTL,
        )
        db.add(grant)
        await db.flush()
        return handle, Pending(grant=grant, client=client)

    def error_redirect(self, redirect_uri: str, error: OAuthError, state: str | None) -> str:
        return with_query(
            redirect_uri,
            {"error": error.error, "error_description": error.description, "state": state,
             "iss": self.issuer},
        )  # fmt: skip

    async def pending(self, db: AsyncSession, handle: str) -> Pending:
        grant = await db.scalar(select(OAuthGrant).where(OAuthGrant.request_hash == _hash(handle)))
        if (
            grant is None
            or grant.status != GrantStatus.PENDING
            or grant.expires_at <= self._clock()
        ):
            raise OAuthError(
                "invalid_request",
                "This sign-in request has expired. Start again from your editor.",
                redirect=False,
            )
        return Pending(grant=grant, client=await self._client_of(db, grant.client_id))

    async def decide(
        self, db: AsyncSession, handle: str, access: OrgAccess, *, approve: bool
    ) -> str:
        """The user's answer: where to send the browser (the client, with a code or
        ``access_denied``)."""
        found = await self.pending(db, handle)
        grant = found.grant
        if not approve:
            grant.status = GrantStatus.DENIED
            await db.flush()
            return self.error_redirect(
                grant.redirect_uri,
                OAuthError("access_denied", "The user said no."),
                grant.state,
            )
        code = secrets.token_urlsafe(32)
        grant.status = GrantStatus.APPROVED
        grant.user_id = access.user.id
        grant.org_id = access.org.id
        grant.code_hash = _hash(code)
        grant.expires_at = self._clock() + CODE_TTL
        await db.flush()
        await audit(
            db, AuditAction.OAUTH_AUTHORIZE, actor_user_id=access.user.id, org_id=access.org.id,
            target_type="oauth_client", target_id=found.client.id,
            meta={"client": found.client.client_name, "scopes": grant.scopes,
                  "resource": grant.resource},
        )  # fmt: skip
        return with_query(
            grant.redirect_uri, {"code": code, "state": grant.state, "iss": self.issuer}
        )

    async def exchange_code(
        self,
        db: AsyncSession,
        *,
        client_id: str | None,
        code: str | None,
        code_verifier: str | None,
        redirect_uri: str | None,
        resource: str | None,
    ) -> Issued:
        client = await self._token_client(db, client_id)
        if not code or not code_verifier:
            raise OAuthError("invalid_request", "code and code_verifier are required.")
        grant = await db.scalar(select(OAuthGrant).where(OAuthGrant.code_hash == _hash(code)))
        if grant is None or grant.client_id != client.id:
            raise OAuthError("invalid_grant", "Unknown code.")
        if grant.status == GrantStatus.USED:
            # Replayed: whatever it bought is revoked (RFC 9700 §4.5).
            await self._revoke_family(db, grant.id)
            raise OAuthError("invalid_grant", "This code was already used.")
        if grant.status != GrantStatus.APPROVED or grant.expires_at <= self._clock():
            raise OAuthError("invalid_grant", "This code has expired.")
        if redirect_uri is not None and redirect_uri != grant.redirect_uri:
            raise OAuthError("invalid_grant", "The redirect_uri differs from the authorization's.")
        if not pkce_ok(code_verifier, grant.code_challenge):
            raise OAuthError("invalid_grant", "The code_verifier doesn't match.")
        if (
            resource is not None
            and self.resources.canonical(resource, default=grant.resource) != grant.resource
        ):
            raise OAuthError("invalid_target", "The resource differs from the authorization's.")
        grant.status = GrantStatus.USED
        user_id, org_id = _decided(grant.user_id, grant.org_id)
        return await self._issue(
            db, client=client, user_id=user_id, org_id=org_id, scopes=grant.scopes,
            resource=grant.resource, family=grant.id,
        )  # fmt: skip

    # ---------------------------------------------------------------- refresh

    async def refresh(
        self,
        db: AsyncSession,
        *,
        client_id: str | None,
        refresh_token: str | None,
        scope: str | None,
        resource: str | None,
    ) -> Issued:
        client = await self._token_client(db, client_id)
        if not refresh_token:
            raise OAuthError("invalid_request", "refresh_token is required.")
        token = await db.scalar(
            select(IssuedToken).where(
                IssuedToken.token_hash == _hash(refresh_token),
                IssuedToken.kind == TokenKind.REFRESH,
            )
        )
        now = self._clock()
        if token is None or token.client_id != client.id or token.revoked_at is not None:
            raise OAuthError("invalid_grant", "Unknown or revoked refresh token.")
        if token.used_at is not None:
            await self._revoke_family(db, token.family_id)
            await set_org_context(db, token.org_id)
            await audit(
                db, AuditAction.OAUTH_REFRESH_REUSED, actor_user_id=token.user_id,
                org_id=token.org_id, target_type="oauth_client", target_id=client.id,
                meta={"client": client.client_name},
            )  # fmt: skip
            raise OAuthError("invalid_grant", "This refresh token was already used.")
        if token.expires_at <= now:
            raise OAuthError("invalid_grant", "This refresh token has expired.")
        scopes = token.scopes if not scope else parse_scopes(scope)
        if not set(scopes) <= set(token.scopes):
            raise OAuthError("invalid_scope", "A refresh can't widen the scopes.")
        if (
            resource is not None
            and self.resources.canonical(resource, default=token.resource) != token.resource
        ):
            raise OAuthError("invalid_target", "The resource differs from the token's.")
        token.used_at = now
        return await self._issue(
            db, client=client, user_id=token.user_id, org_id=token.org_id, scopes=scopes,
            resource=token.resource, family=token.family_id,
        )  # fmt: skip

    # ---------------------------------------------------------------- device flow

    async def device_authorize(
        self,
        db: AsyncSession,
        *,
        client_id: str | None,
        scope: str | None,
        resource: str | None,
    ) -> dict[str, object]:
        client = await self._token_client(db, client_id)
        if GRANT_DEVICE not in grant_types(client):
            raise OAuthError("unauthorized_client", "This client can't use the device flow.")
        scopes = parse_scopes(scope)
        target = self.resources.canonical(resource, default=self.resources.api)
        device_code = _secret(DEVICE_PREFIX)
        for _ in range(5):  # a user code taken: another
            user_code = new_user_code()
            taken = await db.scalar(
                select(OAuthDeviceCode.id).where(OAuthDeviceCode.user_code == user_code)
            )
            if taken is None:
                break
        else:  # pragma: no cover - 20^8 codes
            raise OAuthError("server_error", "Try again.", status=500)
        db.add(
            OAuthDeviceCode(
                device_code_hash=_hash(device_code), user_code=user_code, client_id=client.id,
                scopes=scopes, resource=target, status=GrantStatus.PENDING,
                interval=DEVICE_INTERVAL, expires_at=self._clock() + DEVICE_TTL,
            )
        )  # fmt: skip
        await db.flush()
        shown = show_user_code(user_code)
        return {
            "device_code": device_code,
            "user_code": shown,
            "verification_uri": f"{self.issuer}/device",
            "verification_uri_complete": f"{self.issuer}/device?code={shown}",
            "expires_in": int(DEVICE_TTL.total_seconds()),
            "interval": DEVICE_INTERVAL,
        }

    async def device_pending(
        self, db: AsyncSession, typed: str
    ) -> tuple[OAuthDeviceCode, OAuthClient]:
        found = await db.scalar(
            select(OAuthDeviceCode).where(OAuthDeviceCode.user_code == normal_user_code(typed))
        )
        if (
            found is None
            or found.status != GrantStatus.PENDING
            or found.expires_at <= self._clock()
        ):
            raise OAuthError(
                "invalid_request", "That code isn't valid or has expired.", redirect=False
            )
        return found, await self._client_of(db, found.client_id)

    async def device_decide(
        self, db: AsyncSession, typed: str, access: OrgAccess, *, approve: bool
    ) -> None:
        found, client = await self.device_pending(db, typed)
        found.status = GrantStatus.APPROVED if approve else GrantStatus.DENIED
        if approve:
            found.user_id = access.user.id
            found.org_id = access.org.id
            await db.flush()
            await audit(
                db, AuditAction.OAUTH_AUTHORIZE, actor_user_id=access.user.id,
                org_id=access.org.id, target_type="oauth_client", target_id=client.id,
                meta={"client": client.client_name, "scopes": found.scopes,
                      "resource": found.resource, "device": True},
            )  # fmt: skip
        await db.flush()

    async def device_poll(
        self, db: AsyncSession, *, client_id: str | None, device_code: str | None
    ) -> Issued:
        client = await self._token_client(db, client_id)
        if not device_code:
            raise OAuthError("invalid_request", "device_code is required.")
        found = await db.scalar(
            select(OAuthDeviceCode).where(OAuthDeviceCode.device_code_hash == _hash(device_code))
        )
        if found is None or found.client_id != client.id or found.status == GrantStatus.USED:
            raise OAuthError("invalid_grant", "Unknown device code.")
        now = self._clock()
        if found.expires_at <= now:
            raise OAuthError("expired_token", "The code expired: sign in again.")
        if found.status == GrantStatus.DENIED:
            raise OAuthError("access_denied", "The user said no.")
        too_soon = found.last_polled_at is not None and now - found.last_polled_at < timedelta(
            seconds=found.interval
        )
        found.last_polled_at = now
        if too_soon:
            found.interval += SLOW_DOWN
            await db.flush()
            raise OAuthError("slow_down", f"Poll every {found.interval} seconds.")
        if found.status == GrantStatus.PENDING:
            await db.flush()
            raise OAuthError("authorization_pending", "Waiting for the user.")
        found.status = GrantStatus.USED
        user_id, org_id = _decided(found.user_id, found.org_id)
        return await self._issue(
            db, client=client, user_id=user_id, org_id=org_id, scopes=found.scopes,
            resource=found.resource, family=found.id,
        )  # fmt: skip

    # ---------------------------------------------------------------- tokens

    async def authenticate(self, db: AsyncSession, token: str, *, resource: str) -> TokenPrincipal:
        """The access token's principal for ``resource``, or :class:`TokenRejected`. Leaves the
        transaction scoped to the token's org."""
        found = await db.scalar(
            select(IssuedToken).where(
                IssuedToken.token_hash == _hash(token), IssuedToken.kind == TokenKind.ACCESS
            )
        )
        if found is None:
            raise TokenRejected("invalid")
        if found.revoked_at is not None:
            raise TokenRejected("revoked")
        if found.expires_at <= self._clock():
            raise TokenRejected("expired")
        if found.resource != resource:
            raise TokenRejected("audience")
        user = await db.get(User, found.user_id)
        if user is None or user.deleted_at is not None:
            raise TokenRejected("invalid")
        await set_org_context(db, found.org_id)
        return TokenPrincipal(token=found, user=user)

    async def revoke(self, db: AsyncSession, token: str | None) -> None:
        """RFC 7009: revoke an access or refresh token (a refresh token: its whole family).
        Unknown tokens are fine: the answer doesn't say."""
        if not token:
            return
        found = await db.scalar(select(IssuedToken).where(IssuedToken.token_hash == _hash(token)))
        if found is None:
            return
        if found.kind == TokenKind.REFRESH:
            await self._revoke_family(db, found.family_id)
        elif found.revoked_at is None:
            found.revoked_at = self._clock()
            await db.flush()

    async def _client_of(self, db: AsyncSession, id_: uuid.UUID) -> OAuthClient:
        client = await db.get(OAuthClient, id_)
        if client is None:  # deleted while the request waited
            raise OAuthError("invalid_client", "Unknown client.", redirect=False)
        return client

    async def _token_client(self, db: AsyncSession, client_id: str | None) -> OAuthClient:
        try:
            return await self.client(db, client_id)
        except OAuthError as error:
            raise OAuthError(error.error, error.description, status=401) from None

    async def _revoke_family(self, db: AsyncSession, family: uuid.UUID) -> None:
        await db.execute(
            update(IssuedToken)
            .where(IssuedToken.family_id == family, IssuedToken.revoked_at.is_(None))
            .values(revoked_at=self._clock())
        )

    async def _issue(
        self,
        db: AsyncSession,
        *,
        client: OAuthClient,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        scopes: Sequence[str],
        resource: str,
        family: uuid.UUID,
    ) -> Issued:
        now = self._clock()
        access = _secret(ACCESS_PREFIX)
        refresh = _secret(REFRESH_PREFIX)
        common = {
            "family_id": family, "client_id": client.id, "user_id": user_id, "org_id": org_id,
            "scopes": list(scopes), "resource": resource,
        }  # fmt: skip
        db.add_all(
            [
                IssuedToken(
                    kind=TokenKind.ACCESS, token_hash=_hash(access),
                    expires_at=now + self._access_ttl, **common,
                ),
                IssuedToken(
                    kind=TokenKind.REFRESH, token_hash=_hash(refresh),
                    expires_at=now + self._refresh_ttl, **common,
                ),
            ]
        )  # fmt: skip
        await db.flush()
        return Issued(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(self._access_ttl.total_seconds()),
            scopes=list(scopes),
        )
