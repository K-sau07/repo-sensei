"""API keys (7.1): made, checked, listed and revoked.

* A key is ``rs_<prefix>_<secret>``: an 8-character prefix and 32 random bytes, both base62. It
  is shown once; only the prefix (to find it) and an argon2id hash of the whole key are kept.
* Checking a key finds it by prefix and verifies the hash (off the event loop: argon2 is slow on
  purpose). A key that isn't there, or isn't one, is verified against a dummy hash all the same,
  so the time taken doesn't say which prefixes exist.
* A key acts as its user, in its org, within its scopes: membership and roles are checked as
  for a browser, live. Revoked and expired keys stop at once. ``last_used_at`` is written at
  most once per ``touch_after`` (not a write per call).
"""

from __future__ import annotations

import asyncio
import enum
import re
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.db.base import new_id
from sensei_core.db.tenancy import set_api_key_context, set_org_context
from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.models import ApiKey, Role, User
from sensei_core.orgs.service import OrgAccess

Clock = Callable[[], datetime]


class Scope(enum.StrEnum):
    READ_REPO = "read:repo"  # search, code, trace, graph, impact
    ASK = "ask"  # ask questions (spends the org's model budget)
    FIX = "fix"  # later (phase 8)
    ADMIN = "admin"  # none for now


GRANTABLE = frozenset({Scope.READ_REPO, Scope.ASK})
EXPIRY_DAYS = frozenset({30, 90, 365})
PREFIX_LENGTH = 8
SECRET_BYTES = 32
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
KEY_RE = re.compile(r"^rs_([0-9A-Za-z]{8})_([0-9A-Za-z]{40,48})$")
# OWASP's argon2id floor (19 MiB, 2 passes): a key is 256 random bits, so the hash guards a
# leaked table, not a guessable secret; a check stays in the tens of milliseconds.
_HASHER = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
_DUMMY = _HASHER.hash("rs_00000000_" + "0" * 43)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _base62(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out: list[str] = []
    while n:
        n, digit = divmod(n, 62)
        out.append(_ALPHABET[digit])
    return "".join(reversed(out)) or "0"


def new_key() -> tuple[str, str]:
    """A new key and its prefix."""
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(PREFIX_LENGTH))
    secret = _base62(secrets.token_bytes(SECRET_BYTES)).rjust(43, "0")
    return f"rs_{prefix}_{secret}", prefix


def _verify(key_hash: str, key: str) -> bool:
    try:
        return _HASHER.verify(key_hash, key)
    except (VerificationError, InvalidHashError):
        return False


@dataclass(frozen=True, slots=True)
class KeyPrincipal:
    """A checked key and the user it acts as."""

    key: ApiKey
    user: User

    def allows(self, scope: Scope) -> bool:
        return scope.value in self.key.scopes


class KeyRejected(Exception):
    """A key that is no key, or one that was revoked or has expired (``reason``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # invalid | revoked | expired


class ApiKeyService:
    def __init__(
        self, *, clock: Clock = utcnow, touch_after: timedelta = timedelta(minutes=5)
    ) -> None:
        self._clock = clock
        self._touch_after = touch_after

    async def create(
        self,
        db: AsyncSession,
        access: OrgAccess,
        *,
        name: str,
        scopes: Sequence[str],
        expires_in_days: int | None,
    ) -> tuple[ApiKey, str]:
        """A new key for the caller in this org, and the key itself (shown once)."""
        name = name.strip()
        if not name or len(name) > 100:
            raise InvalidError("Name the key (100 characters at most).", code="invalid_name")
        wanted = sorted(set(scopes))
        if not wanted or any(s not in GRANTABLE for s in wanted):
            raise InvalidError(
                f"Choose scopes from: {', '.join(sorted(GRANTABLE))}.", code="invalid_scope"
            )
        if expires_in_days is not None and expires_in_days not in EXPIRY_DAYS:
            raise InvalidError(
                "A key expires in 30, 90 or 365 days, or never.", code="invalid_expiry"
            )
        token, prefix = new_key()
        key_hash = await asyncio.to_thread(_HASHER.hash, token)
        now = self._clock()
        key = ApiKey(
            id=new_id(),
            org_id=access.org.id,
            user_id=access.user.id,
            name=name,
            prefix=prefix,
            key_hash=key_hash,
            scopes=wanted,
            expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None,
            created_at=now,
        )
        db.add(key)
        await db.flush()
        await audit(
            db,
            AuditAction.API_KEY_CREATE,
            actor_user_id=access.user.id,
            org_id=access.org.id,
            target_type="api_key",
            target_id=key.id,
            meta={"name": name, "prefix": prefix, "scopes": wanted, "expires_at": key.expires_at},
        )
        return key, token

    async def authenticate(self, db: AsyncSession, token: str) -> KeyPrincipal:
        """The key's principal, or :class:`KeyRejected`. Leaves the transaction scoped to the
        key's org."""
        match = KEY_RE.match(token)
        key: ApiKey | None = None
        if match:
            await set_api_key_context(db, match.group(1))
            key = await db.scalar(select(ApiKey).where(ApiKey.prefix == match.group(1)))
        ok = await asyncio.to_thread(_verify, key.key_hash if key else _DUMMY, token)
        if key is None or not ok:
            raise KeyRejected("invalid")
        now = self._clock()
        if key.revoked_at is not None:
            raise KeyRejected("revoked")
        if key.expires_at is not None and key.expires_at <= now:
            raise KeyRejected("expired")
        user = await db.get(User, key.user_id)
        if user is None or user.deleted_at is not None:
            raise KeyRejected("invalid")
        await set_org_context(db, key.org_id)
        if key.last_used_at is None or now - key.last_used_at >= self._touch_after:
            await db.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now))
            key.last_used_at = now
        return KeyPrincipal(key=key, user=user)

    async def list(self, db: AsyncSession, access: OrgAccess) -> list[ApiKey]:
        """The org's keys for an admin; a member's own otherwise. Newest first."""
        query = select(ApiKey).order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        if not access.role.at_least(Role.ADMIN):
            query = query.where(ApiKey.user_id == access.user.id)
        return list((await db.scalars(query)).all())

    async def revoke(self, db: AsyncSession, access: OrgAccess, key_id: uuid.UUID) -> ApiKey:
        """Revoke a key: the caller's own, or any in the org for an admin. Revoking twice is
        a no-op."""
        key = await db.get(ApiKey, key_id)
        mine = key is not None and key.user_id == access.user.id
        if key is None or not (mine or access.role.at_least(Role.ADMIN)):
            raise NotFoundError("No such API key.", code="api_key_not_found")
        if key.revoked_at is None:
            key.revoked_at = self._clock()
            await db.flush()
            await audit(
                db,
                AuditAction.API_KEY_REVOKE,
                actor_user_id=access.user.id,
                org_id=access.org.id,
                target_type="api_key",
                target_id=key.id,
                meta={"name": key.name, "prefix": key.prefix, "owner": key.user_id},
            )
        return key
