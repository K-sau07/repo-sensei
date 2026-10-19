"""Linking GitHub App installations to orgs.

1. An org admin starts an install: ``start()`` stores a random, single-use ``state`` naming the org
   and the admin, and returns GitHub's install page URL carrying it.
2. GitHub sends the browser back to the Setup URL with ``installation_id`` and ``state``.
   ``consume_state()`` checks the state was issued to the signed-in user (so a link sent to someone
   else can't attach *their* installation to the sender's org).
3. GitHub warns that ``installation_id`` can be spoofed, so ``link()`` only trusts it after the
   user's own GitHub token lists that installation (``GET /user/installations``), then reads the
   details as the app and records the link.

An installation belongs to one org. Row-level security hides other orgs' rows, so a link that
collides with another org's is detected by the unique index, without reading that row.
"""

from __future__ import annotations

import enum
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlencode

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.config import GitHubSettings
from sensei_core.errors import ConflictError, DomainError, ForbiddenError, NotFoundError
from sensei_core.github.app import GitHubApp
from sensei_core.github.client import Credential, as_object
from sensei_core.models import GitHubInstallation, Role, User
from sensei_core.orgs.service import OrgAccess, require_role

# Choosing repositories on GitHub can take a while; the state is single use either way.
INSTALL_STATE_TTL_SECONDS = 1800
_STATE_KEY = "github:install:state:{}"


class InstallReturn(enum.StrEnum):
    """Where the browser goes after installing."""

    ONBOARDING = "onboarding"
    REPOS = "repos"


class InstallStateError(DomainError):
    status_code = 400
    code = "invalid_state"


@dataclass(frozen=True, slots=True)
class PendingInstall:
    org_id: uuid.UUID
    org_slug: str
    user_id: uuid.UUID
    return_to: InstallReturn


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def manage_url(settings: GitHubSettings, installation: GitHubInstallation) -> str:
    """Where the account's owner changes repository access or uninstalls the app on GitHub."""
    if installation.account_type == "Organization":
        return (
            f"{settings.web_url}/organizations/{installation.account_login}"
            f"/settings/installations/{installation.installation_id}"
        )
    return f"{settings.web_url}/settings/installations/{installation.installation_id}"


def _details(body: dict[str, Any]) -> dict[str, Any]:
    """Our columns from GitHub's installation object."""
    account = as_object(body.get("account")) or {}
    events = cast("list[object]", body.get("events") or [])
    return {
        # Enterprise installations name the account by slug rather than login.
        "account_login": str(account.get("login") or account.get("slug") or account.get("name"))[
            :100
        ],
        "account_type": str(body.get("target_type") or account.get("type") or "User")[:32],
        "account_id": int(account.get("id") or body.get("target_id") or 0),
        "repository_selection": str(body.get("repository_selection") or "selected")[:16],
        "permissions": dict(body.get("permissions") or {}),
        "events": [str(event)[:64] for event in events],
        "suspended_at": _parse_time(body.get("suspended_at")),
    }


class InstallationService:
    def __init__(self, settings: GitHubSettings, redis: Redis, app: GitHubApp) -> None:
        self._settings = settings
        self._redis = redis
        self._app = app

    async def start(self, access: OrgAccess, return_to: InstallReturn) -> str:
        """The GitHub page that installs the app; only admins may connect accounts."""
        require_role(access, Role.ADMIN)
        state = secrets.token_urlsafe(32)
        pending = {
            "org_id": str(access.org.id),
            "org_slug": access.org.slug,
            "user_id": str(access.user.id),
            "return_to": return_to.value,
        }
        await self._redis.set(
            _STATE_KEY.format(state), json.dumps(pending), ex=INSTALL_STATE_TTL_SECONDS
        )
        query = urlencode({"state": state})
        return f"{self._settings.web_url}/apps/{self._settings.app_slug}/installations/new?{query}"

    async def consume_state(self, state: str, user: User) -> PendingInstall:
        """The install this ``state`` started, if the signed-in ``user`` started it (single use)."""
        raw = await self._redis.getdel(_STATE_KEY.format(state))
        if raw is None:
            raise InstallStateError("This install link expired or was already used.")
        data = json.loads(raw)
        if data["user_id"] != str(user.id):
            raise InstallStateError(
                "This install was started by someone else.", code="state_mismatch"
            )
        return PendingInstall(
            org_id=uuid.UUID(data["org_id"]),
            org_slug=data["org_slug"],
            user_id=user.id,
            return_to=InstallReturn(data["return_to"]),
        )

    async def link(
        self,
        db: AsyncSession,
        access: OrgAccess,
        installation_id: int,
        *,
        user_token: str,
        setup_action: str | None = None,
    ) -> tuple[GitHubInstallation, bool]:
        """Attach ``installation_id`` to the org; returns the row and whether it is new."""
        require_role(access, Role.ADMIN)
        await self._require_user_access(access.user, installation_id, user_token)
        details = _details(await self._app.get_installation(installation_id))

        inserted = await db.scalar(
            pg_insert(GitHubInstallation)
            .values(
                org_id=access.org.id,
                installation_id=installation_id,
                linked_by=access.user.id,
                **details,
            )
            .on_conflict_do_nothing(index_elements=["installation_id"])
            .returning(GitHubInstallation.id)
        )
        row = await db.scalar(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == installation_id)
            .execution_options(populate_existing=True)
        )
        if row is None:  # the unique index matched a row this org cannot see
            raise ConflictError(
                "This GitHub account is already connected to another organization.",
                code="installation_linked_elsewhere",
            )
        created = inserted is not None
        if not created:  # re-linked (e.g. "Redirect on update"): refresh what GitHub told us
            for name, value in details.items():
                setattr(row, name, value)
            row.deleted_at = None
            await db.flush()
        await audit(
            db,
            AuditAction.GITHUB_INSTALL_LINKED,
            actor_user_id=access.user.id,
            org_id=access.org.id,
            target_type="github_installation",
            target_id=row.id,
            meta={
                "installation_id": installation_id,
                "account_login": row.account_login,
                "account_type": row.account_type,
                "repository_selection": row.repository_selection,
                "setup_action": setup_action,
                "created": created,
            },
        )
        return row, created

    async def _require_user_access(self, user: User, installation_id: int, token: str) -> None:
        """GitHub lists only installations the token's user can access; anything else is a
        spoofed or foreign ``installation_id``."""
        credential = Credential(f"Bearer {token}", f"user:{user.id}")
        async for item in self._app.client.paginate(
            "/user/installations", credential, item_key="installations"
        ):
            if (as_object(item) or {}).get("id") == installation_id:
                return
        raise ForbiddenError(
            "Your GitHub account can't access that installation.",
            code="installation_not_accessible",
        )


async def list_org_installations(db: AsyncSession, access: OrgAccess) -> list[GitHubInstallation]:
    """The org's connected GitHub accounts (any member may see them)."""
    rows = await db.scalars(
        select(GitHubInstallation)
        .where(
            GitHubInstallation.org_id == access.org.id,
            GitHubInstallation.deleted_at.is_(None),
        )
        .order_by(GitHubInstallation.account_login)
    )
    return list(rows)


def org_matches(access: OrgAccess, pending: PendingInstall) -> None:
    """The org the install was started for still exists under the slug it had."""
    if access.org.id != pending.org_id:
        raise NotFoundError("Organization not found.", code="org_not_found")
