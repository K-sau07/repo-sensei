"""Sentry (8.3): an org's internal integration, and the webhook it sends.

``PUT/GET/DELETE /orgs/{org}/integrations/sentry`` (admins): the integration's token and client
secret (stored encrypted, never shown again), which Sentry projects are which repositories, and
whether busy issues start a fix by themselves.

``POST /webhooks/sentry/{org}``: Sentry's deliveries. The signature is checked against the org's
client secret before anything else; then it answers at once (Sentry waits about a second) and
the rest happens after: the issue's last 14 days are asked of Sentry, its latest event read,
and a fix made from it, started if the gate says so (:func:`should_auto_run`), else waiting in
the inbox for someone to decide. Deliveries about anything else are acknowledged and dropped.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.deps import DbSession
from sensei_api.errors import AppError
from sensei_api.orgs import OrgAccessDep
from sensei_api.routers.webhooks import read_body
from sensei_core.audit import AuditAction, audit
from sensei_core.db.engine import Database
from sensei_core.db.tenancy import set_org_context
from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.fixes.sentry import (
    SentryClient,
    SentryIssue,
    host_allowed,
    parse_webhook,
    should_auto_run,
    signature_valid,
    ticket_from_event,
)
from sensei_core.fixes.service import FixService, repo_paths
from sensei_core.fixes.tickets import parse_ticket
from sensei_core.jobs import FixJobs
from sensei_core.logging import get_logger
from sensei_core.models import FixSource, FixStatus, Org, Repo, Role, SentryIntegration
from sensei_core.orgs.service import require_role
from sensei_core.security.crypto import TokenCipher

router = APIRouter(tags=["sentry"])
log = get_logger(__name__)
MAX_BODY = 1024 * 1024
SECRET_CONTEXT = b"sentry-integration"


def _context(org_id: uuid.UUID, what: str) -> bytes:
    return SECRET_CONTEXT + b":" + what.encode() + b":" + org_id.bytes


class SentryIn(BaseModel):
    sentry_org: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    api_url: str = Field(default="https://sentry.io", pattern=r"^https://[^\s/]+(/[^\s]*)?$")
    token: str = Field(min_length=8, max_length=500)
    client_secret: str = Field(min_length=8, max_length=500)
    projects: dict[str, str] = Field(default_factory=dict, max_length=200)  # slug -> repo slug
    auto_run: bool = False
    min_events: int = Field(default=10, ge=1, le=100_000)


class SentryOut(BaseModel):
    sentry_org: str
    api_url: str
    projects: dict[str, str]  # Sentry project slug -> repository slug
    auto_run: bool
    min_events: int
    webhook_url: str


def _cipher(request: Request) -> TokenCipher:
    cipher: TokenCipher = request.app.state.token_cipher
    return cipher


def _webhook_url(request: Request, org: str) -> str:
    return f"{request.app.state.settings.web.origin}/api/v1/webhooks/sentry/{org}"


async def _out(db: AsyncSession, request: Request, org: str, found: SentryIntegration) -> SentryOut:
    ids = [uuid.UUID(v) for v in found.projects.values()]
    slugs = {
        row.id: row.slug
        for row in await db.execute(select(Repo.id, Repo.slug).where(Repo.id.in_(ids)))
    }
    projects = {p: slugs[uuid.UUID(r)] for p, r in found.projects.items() if uuid.UUID(r) in slugs}
    return SentryOut(
        sentry_org=found.sentry_org, api_url=found.api_url, projects=projects,
        auto_run=found.auto_run, min_events=found.min_events,
        webhook_url=_webhook_url(request, org),
    )  # fmt: skip


@router.put("/orgs/{org}/integrations/sentry", response_model=SentryOut)
async def put_sentry(
    request: Request, body: SentryIn, access: OrgAccessDep, db: DbSession
) -> SentryOut:
    """Connect Sentry (or change the connection). Admins."""
    require_role(access, Role.ADMIN)
    if not host_allowed(body.api_url, request.app.state.settings.fixer.sentry_hosts):
        raise InvalidError("That isn't a Sentry this server talks to.", code="sentry_host")
    org_id = access.org.id
    repos = {
        row.slug: row.id
        for row in await db.execute(
            select(Repo.id, Repo.slug).where(Repo.org_id == org_id, Repo.deleted_at.is_(None))
        )
    }
    unknown = [r for r in body.projects.values() if r not in repos]
    if unknown:
        raise InvalidError(f"No such repositories: {', '.join(sorted(unknown))}.",
                           code="unknown_repo")  # fmt: skip
    cipher = _cipher(request)
    found = await db.scalar(select(SentryIntegration).where(SentryIntegration.org_id == org_id))
    if found is None:
        found = SentryIntegration(org_id=org_id)
        db.add(found)
    found.sentry_org = body.sentry_org
    found.api_url = body.api_url.rstrip("/")
    found.token_enc = cipher.encrypt(body.token.encode(), context=_context(org_id, "token"))
    found.client_secret_enc = cipher.encrypt(
        body.client_secret.encode(), context=_context(org_id, "secret")
    )
    found.projects = {p: str(repos[r]) for p, r in body.projects.items()}
    found.auto_run = body.auto_run
    found.min_events = body.min_events
    await db.flush()
    await audit(db, AuditAction.SENTRY_CONNECT, actor_user_id=access.user.id, org_id=org_id,
                meta={"sentry_org": body.sentry_org, "auto_run": body.auto_run})  # fmt: skip
    return await _out(db, request, access.org.slug, found)


@router.get("/orgs/{org}/integrations/sentry", response_model=SentryOut)
async def get_sentry(request: Request, access: OrgAccessDep, db: DbSession) -> SentryOut:
    require_role(access, Role.ADMIN)
    found = await db.scalar(
        select(SentryIntegration).where(SentryIntegration.org_id == access.org.id)
    )
    if found is None:
        raise NotFoundError("Sentry isn't connected.", code="sentry_not_connected")
    return await _out(db, request, access.org.slug, found)


@router.delete("/orgs/{org}/integrations/sentry", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sentry(access: OrgAccessDep, db: DbSession) -> None:
    require_role(access, Role.ADMIN)
    found = await db.scalar(
        select(SentryIntegration).where(SentryIntegration.org_id == access.org.id)
    )
    if found is not None:
        await db.delete(found)
        await audit(db, AuditAction.SENTRY_DISCONNECT, actor_user_id=access.user.id,
                    org_id=access.org.id)  # fmt: skip


# ---------------------------------------------------------------- the webhook


def _sessions(request: Request) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Sessions of its own (the work outlives the request's); tests may swap the factory."""
    custom = getattr(request.app.state, "stream_sessions", None)
    if custom is not None:
        return cast("Callable[[], AbstractAsyncContextManager[AsyncSession]]", custom)
    database: Database = request.app.state.database
    return database.session


async def start_from_sentry(
    *,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    cipher: TokenCipher,
    http: httpx.AsyncClient,
    org_id: uuid.UUID,
    resource: str | None,
    payload: dict[str, Any],
    jobs: FixJobs | None = None,
    hosts: Sequence[str] = ("sentry.io",),
    now: datetime | None = None,
) -> uuid.UUID | None:
    """A fix from a verified delivery (its id), or None when it isn't one to make; a started
    one is sent to a worker once it is committed. Runs after the delivery was answered:
    whatever goes wrong is logged with the org and issue (Sentry doesn't retry a 202)."""
    issue = parse_webhook(resource, payload)
    if issue is None:
        return None
    try:
        return await _start(
            sessions, cipher=cipher, http=http, org_id=org_id, issue=issue, jobs=jobs,
            hosts=hosts, now=now,
        )  # fmt: skip
    except Exception:
        log.exception("a Sentry delivery made no fix", org_id=str(org_id), issue=issue.id)
        return None


async def _start(
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    cipher: TokenCipher,
    http: httpx.AsyncClient,
    org_id: uuid.UUID,
    issue: SentryIssue,
    jobs: FixJobs | None,
    hosts: Sequence[str],
    now: datetime | None,
) -> uuid.UUID | None:
    async with sessions() as db:
        await set_org_context(db, org_id)
        found = await db.scalar(select(SentryIntegration).where(SentryIntegration.org_id == org_id))
        repo_id = found.projects.get(issue.project or "") if found else None
        repo = await db.get(Repo, uuid.UUID(repo_id)) if repo_id else None
        if found is None or repo is None or repo.deleted_at is not None:
            log.info("sentry delivery for no connected repository", project=issue.project)
            return None
        if not host_allowed(found.api_url, hosts):
            log.warning("sentry integration points outside the allowed hosts", org_id=str(org_id))
            return None
        service = FixService()
        if await service.open_for(db, repo, FixSource.SENTRY, issue.id) is not None:
            return None  # already being fixed
        token = cipher.decrypt(found.token_enc, context=_context(org_id, "token")).decode()
        client = SentryClient(http, api_url=found.api_url, organization=found.sentry_org,
                              token=token)  # fmt: skip
        events = await client.events_14d(issue.id)
        event = issue.event or await client.latest_event(issue.id)
        paths = await repo_paths(db, repo)
        ticket = (
            ticket_from_event(event, title=issue.title, paths=paths)
            if event
            else parse_ticket(issue.title, title=issue.title, paths=paths)
        )
        go = should_auto_run(
            enabled=found.auto_run, events_14d=events, last_seen=issue.last_seen,
            now=now or datetime.now(UTC), min_events=found.min_events,
        )  # fmt: skip
        run, _ = await service.create(
            db, repo, ticket, source=FixSource.SENTRY, source_ref=issue.id,
            status=FixStatus.QUEUED if go else FixStatus.NEEDS_TRIAGE,
        )  # fmt: skip
        started = (run.org_id, run.id, run.stage.value) if go else None
        made = run.id
    if started is not None and jobs is not None:
        await jobs.run_stage(*started)
    return made


@router.post("/webhooks/sentry/{org}", status_code=status.HTTP_202_ACCEPTED)
async def sentry_webhook(
    *,
    org: str,
    request: Request,
    db: DbSession,
    background: BackgroundTasks,
    sentry_hook_resource: Annotated[str | None, Header(max_length=64)] = None,
    sentry_hook_signature: Annotated[str | None, Header(max_length=200)] = None,
) -> Response:
    body = await read_body(request, MAX_BODY)
    refused = AppError("Invalid signature.", code="invalid_signature", status_code=401)
    org_id = await db.scalar(
        select(Org.id).where(Org.slug == org.lower(), Org.deleted_at.is_(None))
    )
    found = None
    if org_id is not None:
        await set_org_context(db, org_id)
        found = await db.scalar(select(SentryIntegration).where(SentryIntegration.org_id == org_id))
    if org_id is None or found is None:
        # The same answer, after the same work: which orgs exist isn't said.
        signature_valid(secrets.token_hex(16), body, sentry_hook_signature)
        raise refused
    cipher = _cipher(request)
    secret = cipher.decrypt(found.client_secret_enc, context=_context(org_id, "secret")).decode()
    if not signature_valid(secret, body, sentry_hook_signature):
        raise refused
    try:
        parsed: Any = json.loads(body)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise AppError("Invalid JSON.", code="invalid_payload", status_code=400)
    payload = cast("dict[str, Any]", parsed)
    background.add_task(
        start_from_sentry, sessions=_sessions(request), cipher=cipher,
        http=request.app.state.http, org_id=org_id, resource=sentry_hook_resource,
        payload=payload, jobs=request.app.state.fix_jobs,
        hosts=request.app.state.settings.fixer.sentry_hosts,
    )  # fmt: skip
    return Response(status_code=status.HTTP_202_ACCEPTED)
