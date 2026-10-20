"""The audit log: one append-only row per privileged action.

Rows are written in the caller's transaction, so an action and its audit record commit or roll back
together. The caller's IP and request id come from the request context.
"""

from __future__ import annotations

import enum
import json
import uuid
from typing import Any

from sqlalchemy import insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sensei_core.context import current_client_ip, current_context
from sensei_core.db.base import new_id
from sensei_core.db.tenancy import set_user_context
from sensei_core.models import ActorType, AuditEvent, User
from sensei_core.redaction import redact

MAX_PAGE_SIZE = 200


class AuditAction(enum.StrEnum):
    LOGIN = "auth.login"
    LOGOUT = "auth.logout"
    LOGOUT_ALL = "auth.logout_all"
    ORG_CREATE = "org.create"
    ORG_UPDATE = "org.update"
    ORG_DELETE = "org.delete"
    INVITATION_CREATE = "invitation.create"
    INVITATION_REVOKE = "invitation.revoke"
    INVITATION_ACCEPT = "invitation.accept"
    MEMBER_ROLE_CHANGE = "member.role_change"
    MEMBER_REMOVE = "member.remove"
    MEMBER_LEAVE = "member.leave"
    GITHUB_INSTALL_LINKED = "github.install.linked"
    GITHUB_INSTALL_DELETED = "github.install.deleted"
    GITHUB_INSTALL_SUSPENDED = "github.install.suspended"
    GITHUB_INSTALL_UNSUSPENDED = "github.install.unsuspended"
    GITHUB_INSTALL_PERMISSIONS_ACCEPTED = "github.install.permissions_accepted"
    REPO_CONNECTED = "repo.connected"
    REPO_SETTINGS_UPDATE = "repo.settings_update"
    REPO_DISCONNECTED = "repo.disconnected"
    REPO_INDEX_STARTED = "repo.index_started"
    REPO_INDEX_CANCELLED = "repo.index_cancelled"


def _clean_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """JSON-safe (UUIDs, enums and datetimes become strings) and with secrets masked."""
    if not meta:
        return {}
    cleaned: dict[str, Any] = redact(json.loads(json.dumps(meta, default=str)))
    return cleaned


async def audit(
    db: AsyncSession,
    action: AuditAction,
    *,
    actor_user_id: uuid.UUID | None,
    org_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    meta: dict[str, Any] | None = None,
    actor_type: ActorType | None = None,
) -> uuid.UUID:
    """Record ``action``. Org events need the transaction scoped to ``org_id`` already; account
    events (``org_id`` None) are recorded on behalf of the signed-in user."""
    if org_id is None and actor_user_id is not None:
        await set_user_context(db, actor_user_id)
    event_id = new_id()
    await db.execute(
        insert(AuditEvent).values(
            id=event_id,
            org_id=org_id,
            actor_user_id=actor_user_id,
            actor_type=actor_type or (ActorType.USER if actor_user_id else ActorType.SYSTEM),
            action=action.value,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            meta=_clean_meta(meta),
            ip=current_client_ip(),
            request_id=current_context().get("request_id"),
        )
    )
    return event_id


async def list_org_audit(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    action: AuditAction | None = None,
    before: uuid.UUID | None = None,
    limit: int = 50,
) -> list[tuple[AuditEvent, str | None]]:
    """Newest first, with the actor's login. ``before`` is the id of the last row already seen."""
    query = (
        select(AuditEvent, User.login)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .where(AuditEvent.org_id == org_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(max(1, min(limit, MAX_PAGE_SIZE)))
    )
    if action is not None:
        query = query.where(AuditEvent.action == action.value)
    if before is not None:
        seen = aliased(AuditEvent)  # an alias, so the subqueries never correlate to the page
        last_seen = seen.id == before, seen.org_id == org_id
        query = query.where(
            tuple_(AuditEvent.created_at, AuditEvent.id)
            < tuple_(
                select(seen.created_at).where(*last_seen).scalar_subquery(),
                select(seen.id).where(*last_seen).scalar_subquery(),
            )
        )
    rows = await db.execute(query)
    return [(event, login) for event, login in rows.tuples()]
