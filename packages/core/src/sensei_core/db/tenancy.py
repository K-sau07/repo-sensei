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
APP_ROLE = "sensei_app"
SYSTEM_ROLE = "sensei_system"

_POLICY_EXPR = f"org_id = NULLIF(current_setting('{ORG_SETTING}', true), '')::uuid"


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


def disable_org_rls_sql(table: str) -> list[str]:
    """Inverse of :func:`enable_org_rls_sql` (use in migration downgrades)."""
    return [
        f'DROP POLICY IF EXISTS org_isolation ON "{table}"',
        f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY',
    ]


async def set_org_context(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Scope the current transaction to ``org_id``. Resets automatically at commit/rollback."""
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": ORG_SETTING, "value": str(org_id)},
    )


async def current_org(session: AsyncSession) -> uuid.UUID | None:
    value = await session.scalar(text(f"SELECT NULLIF(current_setting('{ORG_SETTING}', true), '')"))
    return uuid.UUID(value) if value else None
