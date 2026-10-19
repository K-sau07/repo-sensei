"""Installing the GitHub App for an org, and the org's connected GitHub accounts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from sensei_api.auth import OptionalSession
from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep, OrgServiceDep
from sensei_api.routers.auth import GitHubOAuthDep
from sensei_core.auth.github_login import fresh_access_token
from sensei_core.config import Settings
from sensei_core.errors import DomainError
from sensei_core.github.app import GitHubApp
from sensei_core.github.client import GitHubClient, GitHubError
from sensei_core.github.etags import RedisETagCache
from sensei_core.github.installations import (
    InstallationService,
    InstallReturn,
    InstallStateError,
    PendingInstall,
    list_org_installations,
    manage_url,
    org_matches,
)
from sensei_core.github.oauth import GitHubOAuth, OAuthError
from sensei_core.logging import get_logger
from sensei_core.models import User
from sensei_core.security.crypto import TokenCipher

router = APIRouter(tags=["github"])
log = get_logger(__name__)


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_github_app(request: Request) -> GitHubApp | None:
    """The process-wide app (its JWT is reused across requests); None when not configured."""
    state = request.app.state
    existing: GitHubApp | None = getattr(state, "github_app", None)
    if existing is not None:
        return existing
    settings = _settings(request)
    if not settings.github.app_configured:
        return None
    redis: Redis = state.redis
    http: httpx.AsyncClient = state.http
    cipher: TokenCipher = state.token_cipher
    client = GitHubClient(settings.github, http, etags=RedisETagCache(redis, cipher))
    state.github_app = GitHubApp(settings.github, client, redis, cipher)
    return state.github_app


GitHubAppDep = Annotated[GitHubApp | None, Depends(get_github_app)]


def get_installations(request: Request, app: GitHubAppDep) -> InstallationService | None:
    if app is None:
        return None
    return InstallationService(_settings(request).github, request.app.state.redis, app)


InstallationsDep = Annotated[InstallationService | None, Depends(get_installations)]


def _return_path(slug: str | None, return_to: InstallReturn, *, ok: bool) -> str:
    if return_to is InstallReturn.ONBOARDING or slug is None:
        # Success moves on to picking repositories; failure stays on the install step.
        return f"/onboarding?step={3 if ok else 2}" + (f"&org={quote(slug)}" if slug else "")
    return f"/{quote(slug)}/repos/new" + ("?installed=1" if ok else "")


def _to_web(request: Request, path: str, error: str | None = None) -> RedirectResponse:
    if error:
        path = f"{path}{'&' if '?' in path else '?'}github_error={quote(error)}"
    return RedirectResponse(f"{_settings(request).web.origin}{path}", status_code=302)


@router.get("/github/install", response_class=RedirectResponse, status_code=302)
async def github_install(
    *,
    request: Request,
    active: OptionalSession,
    db: DbSession,
    orgs: OrgServiceDep,
    installations: InstallationsDep,
    org: Annotated[str, Query(max_length=64, description="Organization slug")],
    return_to: Annotated[InstallReturn, Query(alias="from")] = InstallReturn.REPOS,
) -> RedirectResponse:
    """Send an org admin to GitHub to install the app. Non-admins get 403."""
    if active is None:
        return _to_web(request, f"/login?next={quote(_return_path(org, return_to, ok=False))}")
    if installations is None:
        return _to_web(request, _return_path(org, return_to, ok=False), "github_app_not_configured")
    access = await orgs.access(db, active.user, org)
    url = await installations.start(access, return_to)
    return RedirectResponse(url, status_code=302)


async def _link_pending(
    *,
    request: Request,
    db: DbSession,
    orgs: OrgServiceDep,
    installations: InstallationService,
    oauth: GitHubOAuth,
    user: User,
    pending: PendingInstall,
    installation_id: int | None,
    setup_action: str | None,
) -> str:
    """Link the installation the state was issued for; returns the org's current slug."""
    if setup_action == "request":  # an owner on GitHub must approve the request first
        raise InstallStateError("The install is waiting for approval.", code="install_requested")
    if installation_id is None:
        raise InstallStateError("GitHub did not say what was installed.", code="install_missing")
    access = await orgs.access(db, user, pending.org_slug)
    org_matches(access, pending)
    cipher: TokenCipher = request.app.state.token_cipher
    user_token = await fresh_access_token(db, cipher, oauth, user)
    installation, created = await installations.link(
        db, access, installation_id, user_token=user_token, setup_action=setup_action
    )
    log.info(
        "github installation linked",
        installation_id=installation_id,
        account_login=installation.account_login,
        created=created,
    )
    return access.org.slug


@router.get("/github/setup", response_class=RedirectResponse, status_code=302)
async def github_setup(
    *,
    request: Request,
    active: OptionalSession,
    db: DbSession,
    orgs: OrgServiceDep,
    installations: InstallationsDep,
    oauth: GitHubOAuthDep,
    installation_id: Annotated[int | None, Query(gt=0)] = None,
    setup_action: Annotated[str | None, Query(max_length=32)] = None,
    state: Annotated[str | None, Query(max_length=128)] = None,
) -> RedirectResponse:
    """GitHub's Setup URL: link the new installation to the org that started the install.

    Always answers with a redirect to the web app, carrying ``github_error`` on failure.
    """
    if active is None:
        return _to_web(request, "/login")
    if installations is None or oauth is None:
        return _to_web(request, "/", "github_app_not_configured")
    if not state:
        # Installed or updated from GitHub directly; webhooks keep existing links current.
        return _to_web(request, "/", None if setup_action == "update" else "install_state_missing")

    pending: PendingInstall | None = None
    try:
        pending = await installations.consume_state(state, active.user)
        slug = await _link_pending(
            request=request,
            db=db,
            orgs=orgs,
            installations=installations,
            oauth=oauth,
            user=active.user,
            pending=pending,
            installation_id=installation_id,
            setup_action=setup_action,
        )
    except (DomainError, OAuthError) as exc:
        error = exc.code
    except GitHubError as exc:
        log.warning("github unavailable during install", status=exc.status)
        error = "github_unavailable"
    except httpx.TransportError as exc:
        log.warning("github unreachable during install", error_type=type(exc).__name__)
        error = "github_unavailable"
    else:
        return _to_web(request, _return_path(slug, pending.return_to, ok=True))
    log.warning("github install not linked", reason=error, installation_id=installation_id)
    slug = pending.org_slug if pending else None
    return_to = pending.return_to if pending else InstallReturn.ONBOARDING
    return _to_web(request, _return_path(slug, return_to, ok=False), error)


class InstallationOut(BaseModel):
    id: uuid.UUID
    installation_id: int
    account_login: str
    account_type: str = Field(description='"User" or "Organization"')
    repository_selection: str = Field(description='"all" or "selected"')
    suspended: bool
    manage_url: str = Field(description="Change repository access or uninstall on GitHub.")
    created_at: datetime
    updated_at: datetime


@router.get("/orgs/{org}/github/installations", response_model=list[InstallationOut])
async def list_installations(
    request: Request, access: OrgAccessDep, db: DbSession
) -> list[InstallationOut]:
    """GitHub accounts connected to the org."""
    github = _settings(request).github
    return [
        InstallationOut(
            id=row.id,
            installation_id=row.installation_id,
            account_login=row.account_login,
            account_type=row.account_type,
            repository_selection=row.repository_selection,
            suspended=row.suspended_at is not None,
            manage_url=manage_url(github, row),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in await list_org_installations(db, access)
    ]
