"""OAuth 2.1 (7.2): the clients that ask (editors' MCP clients, the CLI), the authorizations
they ask for, device codes, and the tokens issued.

Like browser sessions these are credentials, not org data: found by the SHA-256 hash of a
random secret (only hashes are kept), so they aren't behind org row-level security. A grant and
its tokens carry the org the user chose on the consent screen, and the resource (audience) they
are for.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin


def _values(members: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in members]


class ClientKind(enum.StrEnum):
    DYNAMIC = "dynamic"  # registered at /oauth/register (RFC 7591)
    METADATA_DOCUMENT = "metadata_document"  # its client_id is the URL of its metadata


class GrantStatus(enum.StrEnum):
    PENDING = "pending"  # waiting for the user
    APPROVED = "approved"  # a code (or, for a device, tokens) to be had
    DENIED = "denied"
    USED = "used"  # exchanged: once only


class TokenKind(enum.StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class OAuthClient(IdMixin, TimestampMixin, Base):
    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[ClientKind] = mapped_column(
        Enum(ClientKind, name="oauth_client_kind", values_callable=_values)
    )
    client_name: Mapped[str] = mapped_column(String(200))
    client_uri: Mapped[str | None] = mapped_column(Text)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # A metadata document's: when it was read, and until when that reading holds.
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fresh_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthGrant(IdMixin, Base):
    """One authorization request: asked for, decided by the user, then exchanged for tokens."""

    __tablename__ = "oauth_grants"

    request_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("oauth_clients.id", ondelete="CASCADE"), index=True
    )
    redirect_uri: Mapped[str] = mapped_column(Text)
    # Whether the request named its redirect URI (then the token request must too: OAuth 2.1).
    redirect_given: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    resource: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(Text)
    status: Mapped[GrantStatus] = mapped_column(
        Enum(GrantStatus, name="oauth_grant_status", values_callable=_values),
        server_default=GrantStatus.PENDING.value,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    code_hash: Mapped[bytes | None] = mapped_column(LargeBinary, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class OAuthDeviceCode(IdMixin, Base):
    """The device flow (RFC 8628): a code the CLI polls with and one the user types."""

    __tablename__ = "oauth_device_codes"

    device_code_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    user_code: Mapped[str] = mapped_column(String(16), unique=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("oauth_clients.id", ondelete="CASCADE"), index=True
    )
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    resource: Mapped[str] = mapped_column(Text)
    status: Mapped[GrantStatus] = mapped_column(
        Enum(GrantStatus, name="oauth_grant_status", values_callable=_values),
        server_default=GrantStatus.PENDING.value,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    interval: Mapped[int] = mapped_column(Integer)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class IssuedToken(IdMixin, Base):
    """An access or refresh token. A refresh token is used once: each use issues the next of
    its family, and using one twice revokes the family (it was stolen)."""

    __tablename__ = "oauth_issued_tokens"

    kind: Mapped[TokenKind] = mapped_column(
        Enum(TokenKind, name="oauth_token_kind", values_callable=_values)
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("oauth_clients.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    resource: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
