"""Multi-tenancy: org scoping enforced by Postgres row-level security (RLS).

Every org-scoped table carries ``org_id`` and an ``org_isolation`` policy that only exposes rows
whose ``org_id`` matches the transaction-local setting ``app.org_id``. The application role
(``sensei_app``) cannot bypass RLS, so a query that forgets to filter by org still cannot read or
write another org's data. With no org set, org-scoped tables look empty.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

ORG_SETTING = "app.org_id"
USER_SETTING = "app.user_id"
INVITE_SETTING = "app.invite_token_hash"
APP_ROLE = "sensei_app"
SYSTEM_ROLE = "sensei_system"

_POLICY_EXPR = f"org_id = NULLIF(current_setting('{ORG_SETTING}', true), '')::uuid"
_USER_EXPR = f"user_id = NULLIF(current_setting('{USER_SETTING}', true), '')::uuid"
_ACTOR_EXPR = f"actor_user_id = NULLIF(current_setting('{USER_SETTING}', true), '')::uuid"
_INVITE_EXPR = f"token_hash = decode(NULLIF(current_setting('{INVITE_SETTING}', true), ''), 'hex')"


class OrgScopedMixin:
    """Adds a non-null, indexed ``org_id`` referencing ``orgs.id`` (deleted with the org).

    Pair every such table with :func:`enable_org_rls_sql` in its migration. The foreign key is
    resolved lazily, so models may be declared before the ``orgs`` model is imported.
    """

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)


def enable_org_rls_sql(table: str) -> list[str]:
    """SQL statements that turn on org isolation for ``table`` (use in migrations)."""
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY org_isolation ON "{table}" USING ({_POLICY_EXPR}) '
        f"WITH CHECK ({_POLICY_EXPR})",
    ]


def memberships_rls_sql(table: str = "memberships") -> list[str]:
    """Memberships: the current org's rows, plus the signed-in user's own rows in any org
    (so "list my orgs" works before an org is chosen). Writes still require the org context."""
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY org_isolation ON "{table}" USING ({_POLICY_EXPR}) '
        f"WITH CHECK ({_POLICY_EXPR})",
        f'CREATE POLICY own_memberships ON "{table}" FOR SELECT USING ({_USER_EXPR})',
    ]


def invitations_rls_sql(table: str = "invitations") -> list[str]:
    """Invitations: the current org's rows, plus the single row whose token hash the caller
    presents (so an invitee can preview and accept before joining). Writes require the org."""
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY org_isolation ON "{table}" USING ({_POLICY_EXPR}) '
        f"WITH CHECK ({_POLICY_EXPR})",
        f'CREATE POLICY invite_by_token ON "{table}" FOR SELECT USING ({_INVITE_EXPR})',
    ]


def audit_log_rls_sql(table: str = "audit_log") -> list[str]:
    """Audit log: the current org's rows, plus the signed-in user's own account-level events
    (``org_id`` null, e.g. sign-ins), which only that user can write or read. Other users'
    account events are reachable only through the system role (staff tools)."""
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY org_isolation ON "{table}" USING ({_POLICY_EXPR}) '
        f"WITH CHECK ({_POLICY_EXPR})",
        f'CREATE POLICY own_account_events ON "{table}" '
        f"USING (org_id IS NULL AND {_ACTOR_EXPR}) WITH CHECK (org_id IS NULL AND {_ACTOR_EXPR})",
    ]


def append_only_sql(table: str) -> list[str]:
    """The app role may insert and read rows of ``table`` but never change or remove them."""
    return [f'REVOKE UPDATE, DELETE, TRUNCATE ON "{table}" FROM {APP_ROLE}']


def insert_only_sql(table: str) -> list[str]:
    """The app role may add rows to ``table`` but not read, change or remove them (inboxes that
    only workers and staff read, through the system role)."""
    return [f'REVOKE SELECT, UPDATE, DELETE, TRUNCATE ON "{table}" FROM {APP_ROLE}']


def disable_org_rls_sql(table: str) -> list[str]:
    """Inverse of the ``*_rls_sql`` helpers above (use in migration downgrades)."""
    return [
        f'DROP POLICY IF EXISTS org_isolation ON "{table}"',
        f'DROP POLICY IF EXISTS own_memberships ON "{table}"',
        f'DROP POLICY IF EXISTS invite_by_token ON "{table}"',
        f'DROP POLICY IF EXISTS own_account_events ON "{table}"',
        f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY',
    ]


async def set_org_context(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Scope the current transaction to ``org_id``. Resets automatically at commit/rollback."""
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": ORG_SETTING, "value": str(org_id)},
    )


async def set_user_context(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Let the current transaction see the signed-in user's own membership rows."""
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": USER_SETTING, "value": str(user_id)},
    )


async def set_invite_context(session: AsyncSession, token_hash: bytes) -> None:
    """Let the current transaction read the one invitation matching ``token_hash``."""
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": INVITE_SETTING, "value": token_hash.hex()},
    )


async def current_org(session: AsyncSession) -> uuid.UUID | None:
    value = await session.scalar(text(f"SELECT NULLIF(current_setting('{ORG_SETTING}', true), '')"))
    return uuid.UUID(value) if value else None
