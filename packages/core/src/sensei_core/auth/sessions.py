"""Browser sessions.

* The session token and the CSRF token are 32 random bytes each, given to the browser once and
  stored only as SHA-256 hashes, so a database leak does not leak usable sessions.
* Sessions slide: each use extends the idle expiry (written at most once per
  ``session_touch_after_seconds`` to avoid a write per request), up to an absolute lifetime.
* Revocation is immediate (``revoked_at``), per session or for all of a user's sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import AuthSettings
from sensei_core.db.engine import rowcount
from sensei_core.models import User, UserSession

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """What the browser receives once; only hashes are persisted."""

    session: UserSession
    token: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class ActiveSession:
    session: UserSession
    user: User


class SessionService:
    def __init__(self, settings: AuthSettings, *, clock: Clock = utcnow) -> None:
        self._settings = settings
        self._clock = clock

    async def create(
        self,
        db: AsyncSession,
        user: User,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        now = self._clock()
        token, csrf_token = new_token(), new_token()
        session = UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            csrf_hash=hash_token(csrf_token),
            created_at=now,
            last_seen_at=now,
            idle_expires_at=now + timedelta(days=self._settings.session_idle_days),
            absolute_expires_at=now + timedelta(days=self._settings.session_absolute_days),
            ip=ip,
            user_agent=(user_agent or "")[:512] or None,
        )
        db.add(session)
        await db.flush()
        return IssuedSession(session=session, token=token, csrf_token=csrf_token)

    async def authenticate(self, db: AsyncSession, token: str) -> ActiveSession | None:
        """Return the live session and user for ``token``, sliding its idle expiry."""
        now = self._clock()
        row = (
            await db.execute(
                select(UserSession, User)
                .join(User, User.id == UserSession.user_id)
                .where(
                    UserSession.token_hash == hash_token(token),
                    UserSession.revoked_at.is_(None),
                    UserSession.idle_expires_at > now,
                    UserSession.absolute_expires_at > now,
                    User.deleted_at.is_(None),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        session, user = row[0], row[1]
        if now - session.last_seen_at >= timedelta(
            seconds=self._settings.session_touch_after_seconds
        ):
            session.last_seen_at = now
            session.idle_expires_at = min(
                now + timedelta(days=self._settings.session_idle_days),
                session.absolute_expires_at,
            )
            await db.flush()
        return ActiveSession(session=session, user=user)

    @staticmethod
    def csrf_matches(session: UserSession, csrf_token: str) -> bool:
        return hmac.compare_digest(session.csrf_hash, hash_token(csrf_token))

    async def list_active(self, db: AsyncSession, user_id: uuid.UUID) -> list[UserSession]:
        now = self._clock()
        result = await db.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.idle_expires_at > now,
                UserSession.absolute_expires_at > now,
            )
            .order_by(UserSession.last_seen_at.desc())
        )
        return list(result.all())

    async def revoke(self, db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
        """Revoke one of the user's sessions. Returns False if it was not theirs or not live."""
        result = await db.execute(
            update(UserSession)
            .where(
                UserSession.id == session_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=self._clock())
        )
        return rowcount(result) > 0

    async def revoke_all(self, db: AsyncSession, user_id: uuid.UUID) -> int:
        result = await db.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=self._clock())
        )
        return rowcount(result)
