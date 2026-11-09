"""Who may call what (10.10): every operation in the API's OpenAPI document, for each kind of
caller, against a declarative table of who is let through.

The callers: ``anonymous``; ``outsider`` (signed in, a member of another org only); ``viewer``,
``member``, ``admin`` and ``owner`` of the org the request names; and ``staff`` (signed in, staff,
in no org). :data:`MATRIX` says, per operation, what each gets. An operation the document has
and the table doesn't (a new endpoint) fails :func:`test_every_operation_is_classified` until it
is classified, and so does one the table names that the document no longer has.

``OK`` is "let through": the caller may still be turned down for what the request asks (404 for
a missing row, 409, 422 for a body the endpoint refuses, 502/503 for a service tests don't
configure), but not for who they are, and never with a 5xx crash. A refusal is the API's
convention: 401 signed out, 404 ``org_not_found`` for an org the caller isn't in (an org's
existence isn't revealed), 403 for a role below the endpoint's, 404 for staff routes.

Requests are real: each caller has a browser session and sends the CSRF headers, path parameters
name real rows (the caller's own conversation, a fresh member to remove, a fresh key of someone
else's to revoke...), and bodies are made from the operation's request schema or, where the
schema can't make one the endpoint takes as far as its role check, written out in
:data:`BODIES`. Every operation gets a world of its own. Nothing leaves the process: every
outgoing HTTP request (GitHub, Anthropic, Voyage) is answered by a mock refusing it, background
jobs are recorded instead of sent, mail stays in memory.

``AUTHZ_RECORD=1`` prints what every caller gets for every operation instead of checking.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import uuid
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    MutableMapping,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import Any
from urllib.parse import urlencode

import httpx
import orjson
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.app import create_app
from sensei_api.auth import cookie_names
from sensei_api.sharing import new_share_token, share_token_hash
from sensei_core.auth.api_keys import GRANTABLE, ApiKeyService
from sensei_core.auth.github_login import store_tokens
from sensei_core.auth.sessions import SessionService
from sensei_core.config import GitHubSettings, Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.oauth import GitHubTokens
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    AgentRun,
    Conversation,
    ConversationMessage,
    Diagram,
    DiagramKind,
    DiagramSource,
    FixKind,
    FixRun,
    FixSource,
    FixStage,
    FixStatus,
    IndexJob,
    JobKind,
    JobStatus,
    Membership,
    Org,
    Repo,
    RepoSource,
    Role,
    UsageEvent,
    UsageKind,
    User,
)
from sensei_core.orgs.service import OrgAccess, OrgService
from sensei_core.security.crypto import TokenCipher
from sensei_testing import make_user

pytestmark = pytest.mark.integration

API = "/api/v1"
PRINCIPALS: tuple[str, ...] = (
    "anonymous",
    "outsider",
    "viewer",
    "member",
    "admin",
    "owner",
    "staff",
)
ROLES = {"viewer": Role.VIEWER, "member": Role.MEMBER, "admin": Role.ADMIN, "owner": Role.OWNER}
OK = "ok"
NO_ORG = (404, "org_not_found")
Expect = str | int | tuple[int, str]  # OK; or the refusal's status (and code)
Rule = Mapping[str, Expect]


# ----------------------------------------------------------------------------- the rules


def everyone() -> Rule:
    """No sign-in asked (health, share links, the public gallery, OAuth's client side)."""
    return dict.fromkeys(PRINCIPALS, OK)


def signed_in() -> Rule:
    """Any signed-in user, whatever their orgs (their own account, creating an org)."""
    return {p: (401 if p == "anonymous" else OK) for p in PRINCIPALS}


def org(minimum: Role = Role.VIEWER) -> Rule:
    """Members of the org the request names, with at least ``minimum``; to anyone else it
    isn't there (staff included: they are in no org)."""
    rule: dict[str, Expect] = {"anonymous": 401, "outsider": NO_ORG, "staff": NO_ORG}
    for name, role in ROLES.items():
        rule[name] = OK if role.at_least(minimum) else 403
    return rule


def staff() -> Rule:
    """Staff only; to everyone else signed in, the route doesn't exist."""
    return {
        p: 401 if p == "anonymous" else OK if p == "staff" else (404, "not_found")
        for p in PRINCIPALS
    }


def but(rule: Rule, **changes: Expect) -> Rule:
    return {**rule, **changes}


VIEWER, MEMBER, ADMIN, OWNER = org(Role.VIEWER), org(Role.MEMBER), org(Role.ADMIN), org(Role.OWNER)
# Renaming, deleting or sharing a diagram: whoever drew it, or an admin (here: someone else's).
CREATOR_OR_ADMIN = but(ADMIN, viewer=(403, "diagram_not_yours"), member=(403, "diagram_not_yours"))
# Revoking an API key: its own user, or an admin; someone else's isn't there for the rest.
KEY_OWNER_OR_ADMIN = but(
    ADMIN, viewer=(404, "api_key_not_found"), member=(404, "api_key_not_found")
)
# Signature-checked webhooks: no session counts; an unsigned call is refused for everyone.
SIGNED_WEBHOOK: Rule = dict[str, Expect].fromkeys(PRINCIPALS, 401)
# An OAuth consent names the org in its body: the caller must be in it (any role).
CONSENT = but(VIEWER, staff=NO_ORG)
# Restoring a deleted org: its owners; to anyone else it isn't there.
RESTORE = but(OWNER, viewer=NO_ORG, member=NO_ORG, admin=NO_ORG)
# GitHub's install redirects: a signed-out browser is sent to sign in (a redirect, not a 401).
INSTALL = but(ADMIN, anonymous=302)

ORGS = f"{API}/orgs/{{org}}"
REPO = f"{ORGS}/repos/{{repo}}"

MATRIX: dict[str, Rule] = {
    # health, sign-in
    f"GET {API}/healthz": everyone(),
    f"GET {API}/readyz": everyone(),
    f"GET {API}/auth/github/login": everyone(),
    f"GET {API}/auth/github/callback": everyone(),
    f"POST {API}/auth/logout": signed_in(),
    f"POST {API}/auth/logout-all": signed_in(),
    # one's own account
    f"GET {API}/me": signed_in(),
    f"DELETE {API}/me": signed_in(),
    f"PATCH {API}/me": signed_in(),
    f"GET {API}/me/sessions": signed_in(),
    f"DELETE {API}/me/sessions/{{session_id}}": signed_in(),
    f"GET {API}/me/notifications": signed_in(),
    f"PATCH {API}/me/notifications": signed_in(),
    # orgs
    f"GET {API}/orgs": signed_in(),
    f"POST {API}/orgs": signed_in(),
    f"GET {API}/orgs/slug-available": signed_in(),
    f"GET {API}/orgs/deleted": signed_in(),
    f"POST {ORGS}/restore": RESTORE,
    f"GET {ORGS}": VIEWER,
    f"PATCH {ORGS}": ADMIN,
    f"DELETE {ORGS}": OWNER,
    f"GET {ORGS}/members": VIEWER,
    f"PATCH {ORGS}/members/{{user_id}}": ADMIN,
    f"DELETE {ORGS}/members/{{user_id}}": ADMIN,
    f"POST {ORGS}/leave": VIEWER,
    f"GET {ORGS}/invitations": ADMIN,
    f"POST {ORGS}/invitations": ADMIN,
    f"DELETE {ORGS}/invitations/{{invitation_id}}": ADMIN,
    f"GET {ORGS}/audit": ADMIN,
    f"GET {API}/invitations/{{token}}": everyone(),  # the token is the secret
    f"POST {API}/invitations/{{token}}/accept": signed_in(),
    f"GET {ORGS}/home": VIEWER,
    f"GET {ORGS}/events": VIEWER,
    f"GET {ORGS}/usage": ADMIN,
    f"GET {ORGS}/insights": VIEWER,  # costs only shown to admins (checked below)
    f"GET {ORGS}/settings/models": ADMIN,
    f"PATCH {ORGS}/settings/models": ADMIN,
    f"GET {ORGS}/inbox": VIEWER,
    f"POST {ORGS}/inbox/read": VIEWER,
    f"GET {ORGS}/inbox/events": VIEWER,
    # API keys, MCP
    f"POST {ORGS}/api-keys": VIEWER,
    f"GET {ORGS}/api-keys": VIEWER,  # one's own keys; an admin's: everyone's
    f"DELETE {ORGS}/api-keys/{{key_id}}": KEY_OWNER_OR_ADMIN,
    f"POST {ORGS}/mcp/test": VIEWER,
    # GitHub
    f"GET {API}/github/install": INSTALL,
    f"GET {API}/github/setup": everyone(),  # a redirect; links nothing without its state
    f"GET {ORGS}/github/installations": VIEWER,
    f"POST {API}/webhooks/github": everyone(),  # by signature (not configured in tests: 503)
    f"GET {ORGS}/github/repos": ADMIN,
    # repositories
    f"POST {ORGS}/repos": ADMIN,
    f"GET {ORGS}/repos": VIEWER,
    f"POST {ORGS}/repos/import": ADMIN,
    f"POST {ORGS}/repos/estimate": ADMIN,
    f"GET {REPO}": VIEWER,
    f"PATCH {REPO}": ADMIN,
    f"DELETE {REPO}": ADMIN,
    f"PUT {REPO}/published": ADMIN,
    f"DELETE {REPO}/published": ADMIN,
    # indexing
    f"GET {API}/indexing/stages": everyone(),
    f"GET {REPO}/index-jobs": VIEWER,
    f"GET {REPO}/index-jobs/{{job}}": VIEWER,
    f"POST {REPO}/reindex": ADMIN,
    f"POST {REPO}/index-jobs/{{job}}/cancel": ADMIN,
    f"GET {REPO}/index-jobs/{{job}}/events": VIEWER,
    # reading the code
    f"GET {REPO}/tree": VIEWER,
    f"GET {REPO}/file": VIEWER,
    f"GET {REPO}/files/search": VIEWER,
    f"POST {REPO}/grep": VIEWER,
    f"GET {REPO}/constellation": VIEWER,
    f"GET {REPO}/symbols": VIEWER,
    f"GET {REPO}/symbols/at": VIEWER,
    f"GET {REPO}/symbols/outline": VIEWER,
    f"POST {REPO}/search": VIEWER,
    f"GET {REPO}/graph/neighbors": VIEWER,
    f"GET {REPO}/graph/path": VIEWER,
    f"POST {REPO}/trace": VIEWER,
    f"GET {REPO}/trace": VIEWER,
    f"GET {REPO}/feature-maps": VIEWER,
    f"GET {REPO}/overview": VIEWER,
    f"GET {REPO}/refs": VIEWER,
    f"GET {REPO}/staleness": VIEWER,
    f"GET {REPO}/deps": VIEWER,
    f"GET {REPO}/deps/edge-evidence": VIEWER,
    f"GET {REPO}/deps/dsm": VIEWER,
    f"GET {REPO}/deps/cycles": VIEWER,
    f"GET {REPO}/deps/packages": VIEWER,
    f"GET {REPO}/impact": VIEWER,
    f"GET {REPO}/galaxy": VIEWER,
    # asking (one's own conversations; someone else's: checked below)
    f"POST {REPO}/ask": VIEWER,
    f"GET {REPO}/ask/messages/{{message_id}}/stream": VIEWER,
    f"GET {REPO}/conversations": VIEWER,
    f"GET {ORGS}/conversations/{{conversation_id}}": VIEWER,
    f"PATCH {ORGS}/conversations/{{conversation_id}}": VIEWER,
    f"DELETE {ORGS}/conversations/{{conversation_id}}": VIEWER,
    f"POST {ORGS}/conversations/{{conversation_id}}/share": VIEWER,
    f"DELETE {ORGS}/conversations/{{conversation_id}}/share": VIEWER,
    f"POST {ORGS}/messages/{{message_id}}/feedback": VIEWER,
    f"DELETE {ORGS}/messages/{{message_id}}/feedback": VIEWER,
    f"GET {ORGS}/agent-runs/{{run_id}}": VIEWER,
    f"GET {REPO}/runs": VIEWER,
    f"GET {ORGS}/agent-runs/{{run_id}}/events": VIEWER,
    # diagrams (someone else's, for the ones that change it)
    f"POST {REPO}/diagrams": VIEWER,
    f"GET {REPO}/diagrams": VIEWER,
    f"GET {REPO}/diagrams/{{diagram_id}}": VIEWER,
    f"PATCH {REPO}/diagrams/{{diagram_id}}": CREATOR_OR_ADMIN,
    f"DELETE {REPO}/diagrams/{{diagram_id}}": CREATOR_OR_ADMIN,
    f"POST {REPO}/diagrams/{{diagram_id}}/share": CREATOR_OR_ADMIN,
    f"DELETE {REPO}/diagrams/{{diagram_id}}/share": CREATOR_OR_ADMIN,
    # share links, the public gallery
    f"GET {API}/s/{{token}}": everyone(),
    f"GET {API}/d/{{token}}": everyone(),
    f"GET {API}/public/repos": everyone(),
    f"GET {API}/public/repos/{{owner}}/{{name}}": everyone(),
    f"GET {API}/public/repos/{{owner}}/{{name}}/feature-maps": everyone(),
    f"POST {API}/public/repos/{{owner}}/{{name}}/ask": everyone(),
    # fixes and changes: members start and decide them, viewers read them
    f"POST {REPO}/fixes/parse": MEMBER,
    f"POST {REPO}/fixes": MEMBER,
    f"GET {REPO}/fixes": VIEWER,
    f"GET {ORGS}/fixes/{{fix_id}}": VIEWER,
    f"POST {ORGS}/fixes/{{fix_id}}/start": MEMBER,
    f"POST {ORGS}/fixes/{{fix_id}}/cancel": MEMBER,
    f"POST {ORGS}/fixes/{{fix_id}}/retry": MEMBER,
    f"POST {ORGS}/fixes/{{fix_id}}/approval": MEMBER,
    f"GET {ORGS}/fixes/{{fix_id}}/approvals": VIEWER,
    f"GET {ORGS}/fixes/{{fix_id}}/events": VIEWER,
    f"POST {REPO}/changes": MEMBER,
    f"GET {REPO}/changes": VIEWER,
    f"PATCH {ORGS}/changes/{{fix_id}}/plan": MEMBER,
    f"GET {ORGS}/changes/{{fix_id}}": VIEWER,
    f"GET {ORGS}/changes/{{fix_id}}/events": VIEWER,
    f"POST {ORGS}/changes/{{fix_id}}/cancel": MEMBER,
    f"POST {ORGS}/changes/{{fix_id}}/approval": MEMBER,
    f"GET {ORGS}/changes/{{fix_id}}/approvals": VIEWER,
    # integrations
    f"PUT {ORGS}/integrations/sentry": ADMIN,
    f"GET {ORGS}/integrations/sentry": ADMIN,
    f"DELETE {ORGS}/integrations/sentry": ADMIN,
    f"POST {API}/webhooks/sentry/{{org}}": SIGNED_WEBHOOK,
    f"PUT {ORGS}/integrations/slack": ADMIN,
    f"GET {ORGS}/integrations/slack": ADMIN,
    f"DELETE {ORGS}/integrations/slack": ADMIN,
    # staff
    f"GET {API}/internal/evals": staff(),
    f"GET {API}/internal/evals/{{run}}": staff(),
    f"GET {API}/internal/evals/{{run}}/compare": staff(),
    f"GET {API}/internal/admin": staff(),
    f"GET {API}/internal/admin/users": staff(),
    f"GET {API}/internal/admin/orgs": staff(),
    f"GET {API}/internal/admin/jobs": staff(),
    f"POST {API}/internal/admin/jobs/{{job}}/retry": staff(),
    f"POST {API}/internal/admin/jobs/{{job}}/cancel": staff(),
    f"PUT {API}/internal/admin/read-only": staff(),
    # OAuth: the client's side takes no user; the consent pages need a browser session
    f"POST {API}/oauth/register": everyone(),
    f"GET {API}/oauth/authorize": everyone(),
    f"POST {API}/oauth/token": everyone(),
    f"POST {API}/oauth/device/code": everyone(),
    f"POST {API}/oauth/revoke": everyone(),
    f"GET {API}/oauth/requests/{{handle}}": signed_in(),
    f"POST {API}/oauth/requests/{{handle}}/decision": CONSENT,
    f"GET {API}/oauth/device/{{user_code}}": signed_in(),
    f"POST {API}/oauth/device/decision": CONSENT,
}


# What the owner's API key, with every scope a key can be granted (``read:repo``, ``ask``), gets
# (7.1; ``KEY_ACCESS`` in sensei_api.app): the reading routers' GETs and the few POSTs that only
# read or ask. Connecting a repository wants ``connect:repo``, which no key is granted (403
# ``api_key_scope``); OAuth's client side ignores a bearer; every other operation refuses keys
# outright (403 ``api_key_not_allowed``), public ones included.
KEY_OK = {
    f"GET {API}/me",
    f"GET {API}/indexing/stages",
    f"GET {ORGS}/github/repos",  # a read of the repos router: what the org could connect
    f"GET {ORGS}/repos",
    f"GET {REPO}",
    f"GET {REPO}/index-jobs",
    f"GET {REPO}/index-jobs/{{job}}",
    f"GET {REPO}/index-jobs/{{job}}/events",
    f"GET {REPO}/tree",
    f"GET {REPO}/file",
    f"GET {REPO}/files/search",
    f"POST {REPO}/grep",
    f"GET {REPO}/constellation",
    f"GET {REPO}/symbols",
    f"GET {REPO}/symbols/at",
    f"GET {REPO}/symbols/outline",
    f"POST {REPO}/search",
    f"GET {REPO}/graph/neighbors",
    f"GET {REPO}/graph/path",
    f"POST {REPO}/trace",
    f"GET {REPO}/trace",
    f"POST {REPO}/ask",
    f"GET {REPO}/ask/messages/{{message_id}}/stream",
    f"GET {REPO}/conversations",
    f"GET {ORGS}/conversations/{{conversation_id}}",
    f"GET {ORGS}/agent-runs/{{run_id}}",
    f"GET {REPO}/runs",
    f"GET {ORGS}/agent-runs/{{run_id}}/events",
    f"GET {REPO}/feature-maps",
    f"GET {REPO}/overview",
    f"GET {REPO}/refs",
    f"GET {REPO}/staleness",
    f"GET {REPO}/diagrams",
    f"GET {REPO}/diagrams/{{diagram_id}}",
    f"GET {REPO}/deps",
    f"GET {REPO}/deps/edge-evidence",
    f"GET {REPO}/deps/dsm",
    f"GET {REPO}/deps/cycles",
    f"GET {REPO}/deps/packages",
    f"GET {REPO}/impact",
    f"GET {REPO}/galaxy",
    # OAuth's client side: no user, a bearer sent along is ignored
    f"POST {API}/oauth/register",
    f"GET {API}/oauth/authorize",
    f"POST {API}/oauth/token",
    f"POST {API}/oauth/device/code",
    f"POST {API}/oauth/revoke",
}
KEY_SCOPE = {f"POST {ORGS}/repos", f"POST {ORGS}/repos/import", f"POST {ORGS}/repos/estimate"}


def key_rule(name: str) -> Expect:
    if name in KEY_OK:
        return OK
    if name in KEY_SCOPE:
        return (403, "api_key_scope")
    return (403, "api_key_not_allowed")


# ----------------------------------------------------------------------------- the world

_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
)
# GitHub configured (a made-up app; the mock answers for it), so routes that need GitHub get as
# far as their own checks.
GITHUB = GitHubSettings(
    app_id=99,
    app_slug="sensei-test",
    client_id="Iv23.test",
    client_secret=SecretStr("shh"),
    private_key=SecretStr(_PEM),
    max_retries=0,
    rate_limit_max_wait_seconds=0,
)


@dataclass
class World:
    """Org A (one user per role, and an ``author`` member who owns what others act on), org B
    (the outsider's), a staff user in no org, and org A's rows the paths name."""

    db: AsyncSession
    app: FastAPI
    org: Org
    users: dict[str, User]
    author: User
    repo: Repo
    job: IndexJob
    sessions: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    own: dict[tuple[str, str], uuid.UUID] = field(default_factory=dict[tuple[str, str], uuid.UUID])
    invitations: dict[str, str] = field(default_factory=dict[str, str])  # caller -> token
    bearer: str | None = None  # the owner's API key, once made

    @property
    def org_id(self) -> uuid.UUID:
        return self.org.id

    def access(self, user: User, role: Role = Role.OWNER) -> OrgAccess:
        return OrgAccess(org=self.org, role=role, user=user)

    def user(self, principal: str) -> User:
        """The caller's user: the owner's for their API key, the author's for anonymous."""
        return self.users.get("owner" if principal == "key" else principal, self.author)


async def _member(db: AsyncSession, org_id: uuid.UUID, user: User, role: Role) -> None:
    await set_org_context(db, org_id)
    db.add(Membership(org_id=org_id, user_id=user.id, role=role))
    await db.flush()


async def _world(app: FastAPI, db: AsyncSession, base: Settings) -> World:
    service: OrgService = app.state.org_service
    tag = secrets.token_hex(4)
    users = {name: await make_user(db) for name in ("viewer", "member", "admin", "owner")}
    users["outsider"] = await make_user(db)
    users["staff"] = await make_user(db, is_staff=True)
    author = await make_user(db)
    await service.create(db, users["outsider"], name="Other", slug=f"b-{tag}")
    access = await service.create(db, users["owner"], name="Acme", slug=f"a-{tag}")
    org_id = access.org.id
    for name in ("viewer", "member", "admin"):
        await _member(db, org_id, users[name], ROLES[name])
    await _member(db, org_id, author, Role.MEMBER)
    repo = Repo(
        org_id=org_id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=secrets.randbelow(10**9),
        owner=f"own{tag}",
        name="app",
        full_name=f"own{tag}/app",
        slug="app",
        default_branch="main",
        visibility="public",
        published_at=datetime.now(UTC),
    )
    db.add(repo)
    await db.flush()
    job = IndexJob(
        org_id=org_id,
        repo_id=repo.id,
        kind=JobKind.FULL,
        status=JobStatus.FAILED,
        error={"message": "clone failed"},
        finished_at=datetime.now(UTC),
    )
    db.add(job)
    db.add(
        UsageEvent(
            org_id=org_id,
            user_id=users["owner"].id,
            kind=UsageKind.LLM,
            provider="anthropic",
            model="m",
            tokens_in=1,
            tokens_out=1,
            cost_usd=Decimal("0.01"),
            price_version="t",
        )
    )
    await db.flush()
    # The org is the explore org (so publishing is a question of role alone).
    explore = base.explore.model_copy(update={"org": access.org.slug})
    app.state.settings = base.model_copy(update={"github": GITHUB, "explore": explore})
    world = World(db, app, access.org, users, author, repo, job)
    sessions: SessionService = app.state.session_service
    cipher: TokenCipher = app.state.token_cipher
    later = datetime.now(UTC) + timedelta(hours=8)
    for name, user in [*users.items(), ("author", author)]:
        issued = await sessions.create(db, user, ip="127.0.0.1", user_agent="pytest")
        world.sessions[name] = (issued.token, issued.csrf_token)
        tokens = GitHubTokens(
            access_token=f"gho_{secrets.token_hex(8)}",
            refresh_token=None,
            expires_at=later,
            refresh_expires_at=None,
            scopes=[],
        )
        await store_tokens(db, cipher, user, tokens)
    return world


# Path parameters: (world, principal) -> the value. Made fresh per caller where a call changes
# or removes what it names, so one caller's success doesn't decide the next one's outcome.
Param = Callable[[World, str], Awaitable[str]]


async def _talk(world: World, principal: str) -> None:
    """The caller's own conversation, its answer and its agent run (an ask's three rows)."""
    if (principal, "conversation") in world.own:
        return
    user = world.user(principal)
    await set_org_context(world.db, world.org_id)
    talk = Conversation(org_id=world.org_id, repo_id=world.repo.id, user_id=user.id, title="Saving")
    run = AgentRun(
        org_id=world.org_id,
        repo_id=world.repo.id,
        user_id=user.id,
        kind="ask",
        mode="fast",
        status="succeeded",
    )
    world.db.add_all([talk, run])
    await world.db.flush()
    message = ConversationMessage(
        org_id=world.org_id, conversation_id=talk.id, role="assistant", parts=[], run_id=run.id
    )
    world.db.add(message)
    await world.db.flush()
    world.own[(principal, "conversation")] = talk.id
    world.own[(principal, "message")] = message.id
    world.own[(principal, "run")] = run.id


def _own(what: str) -> Param:
    async def get(world: World, principal: str) -> str:
        await _talk(world, principal)
        return str(world.own[(principal, what)])

    return get


def _authors(what: str) -> Param:
    """The author's, not the caller's (for the checks that one's own things are one's own)."""

    async def get(world: World, principal: str) -> str:
        await _talk(world, "author")
        return str(world.own[("author", what)])

    return get


async def _diagram(world: World, principal: str) -> str:
    """A fresh diagram the author drew."""
    await set_org_context(world.db, world.org_id)
    diagram = Diagram(
        org_id=world.org_id,
        repo_id=world.repo.id,
        commit_sha="a" * 40,
        title="Flow",
        kind=DiagramKind.FLOWCHART,
        source=DiagramSource.QUESTION,
        spec={},
        verified_ratio=1.0,
        created_by=world.author.id,
    )
    world.db.add(diagram)
    await world.db.flush()
    return str(diagram.id)


def _fix(kind: FixKind, status: FixStatus, stage: FixStage) -> Param:
    """A fresh fix (or change) the author asked for, at ``status``."""

    async def make(world: World, principal: str) -> str:
        await set_org_context(world.db, world.org_id)
        change = kind is FixKind.CHANGE
        run = FixRun(
            org_id=world.org_id,
            repo_id=world.repo.id,
            kind=kind,
            source=FixSource.REQUEST if change else FixSource.PASTE,
            title="KeyError",
            ticket={"title": "KeyError", "text": "boom"},
            status=status,
            stage=stage,
            plan={"summary": "x", "revision": 1, "files": [], "risk": {"level": "low"}},
            created_by=world.author.id,
        )
        world.db.add(run)
        await world.db.flush()
        return str(run.id)

    return make


async def _target(world: World, principal: str) -> str:
    """A fresh member to change or remove."""
    user = await make_user(world.db)
    await _member(world.db, world.org_id, user, Role.MEMBER)
    return str(user.id)


async def _invitation(world: World, principal: str) -> str:
    service: OrgService = world.app.state.org_service
    await set_org_context(world.db, world.org_id)
    invitation, token = await service.invite(
        world.db,
        world.access(world.users["owner"]),
        role=Role.MEMBER,
        github_login=world.user(principal).login,
    )
    world.invitations[principal] = token
    return str(invitation.id)


async def _invite_token(world: World, principal: str) -> str:
    """A fresh invitation addressed to the caller (by their GitHub login)."""
    await _invitation(world, principal)
    return world.invitations[principal]


async def _api_key(world: World, principal: str) -> str:
    """A fresh key of the author's."""
    service: ApiKeyService = world.app.state.api_key_service
    await set_org_context(world.db, world.org_id)
    key, _ = await service.create(
        world.db,
        world.access(world.author, Role.MEMBER),
        name="ci",
        scopes=["read:repo"],
        expires_in_days=None,
    )
    return str(key.id)


async def _session(world: World, principal: str) -> str:
    """Another session of the caller's own."""
    sessions: SessionService = world.app.state.session_service
    issued = await sessions.create(
        world.db, world.user(principal), ip="127.0.0.1", user_agent="pytest"
    )
    return str(issued.session.id)


async def _shared(world: World, principal: str) -> str:
    """A link to the author's conversation."""
    await _talk(world, "author")
    found = await world.db.get(Conversation, world.own[("author", "conversation")])
    assert found is not None
    token = new_share_token()
    found.share_token_hash = share_token_hash(token)
    await world.db.flush()
    return token


async def _shared_diagram(world: World, principal: str) -> str:
    found = await world.db.get(Diagram, uuid.UUID(await _diagram(world, principal)))
    assert found is not None
    token = new_share_token()
    found.share_token_hash = share_token_hash(token)
    await world.db.flush()
    return token


async def _deleted_org(world: World, principal: str) -> str:
    """A deleted org (within its grace) with every role-holder of org A in it, in that role."""
    service: OrgService = world.app.state.org_service
    slug = f"gone-{secrets.token_hex(4)}"
    access = await service.create(world.db, world.users["owner"], name="Gone", slug=slug)
    for name in ("viewer", "member", "admin"):
        await _member(world.db, access.org.id, world.users[name], ROLES[name])
    access.org.deleted_at = datetime.now(UTC) - timedelta(days=1)
    await world.db.flush()
    return slug


async def _queued_job(world: World, principal: str) -> str:
    """The repository's one active run (the first caller let through cancels it)."""
    if ("world", "queued") not in world.own:
        await set_org_context(world.db, world.org_id)
        job = IndexJob(
            org_id=world.org_id, repo_id=world.repo.id, kind=JobKind.FULL, status=JobStatus.QUEUED
        )
        world.db.add(job)
        await world.db.flush()
        world.own[("world", "queued")] = job.id
    return str(world.own[("world", "queued")])


async def _bearer(world: World) -> str:
    """The owner's API key with every scope a key can have (made once per world)."""
    if world.bearer is None:
        service: ApiKeyService = world.app.state.api_key_service
        await set_org_context(world.db, world.org_id)
        _, token = await service.create(
            world.db,
            world.access(world.users["owner"]),
            name="all",
            scopes=sorted(GRANTABLE),
            expires_in_days=None,
        )
        world.bearer = token
    return world.bearer


def _fixed(value: str) -> Param:
    async def get(world: World, principal: str) -> str:
        return value

    return get


def _attr(get: Callable[[World], object]) -> Param:
    async def value(world: World, principal: str) -> str:
        return str(get(world))

    return value


PARAMS: dict[str, Param] = {
    "org": _attr(lambda w: w.org.slug),
    "repo": _attr(lambda w: w.repo.slug),
    "job": _attr(lambda w: w.job.id),
    "conversation_id": _own("conversation"),
    "message_id": _own("message"),
    "run_id": _own("run"),
    "diagram_id": _diagram,
    "fix_id": _fix(FixKind.FIX, FixStatus.NEEDS_TRIAGE, FixStage.INTAKE),
    "user_id": _target,
    "invitation_id": _invitation,
    "key_id": _api_key,
    "session_id": _session,
    "token": _invite_token,
    "owner": _attr(lambda w: w.repo.owner),
    "name": _attr(lambda w: w.repo.name),
    "run": _fixed(str(uuid.uuid4())),
    "handle": _fixed("h" * 20),
    "user_code": _fixed("ABCD-EFGH"),
}
_AWAITING = (FixStatus.AWAITING_APPROVAL, FixStage.AWAITING_APPROVAL)
OP_PARAMS: dict[str, dict[str, Param]] = {
    f"POST {ORGS}/restore": {"org": _deleted_org},
    f"POST {REPO}/index-jobs/{{job}}/cancel": {"job": _queued_job},
    f"POST {API}/internal/admin/jobs/{{job}}/cancel": {"job": _queued_job},
    f"GET {API}/s/{{token}}": {"token": _shared},
    f"GET {API}/d/{{token}}": {"token": _shared_diagram},
    f"POST {ORGS}/fixes/{{fix_id}}/retry": {
        "fix_id": _fix(FixKind.FIX, FixStatus.FAILED, FixStage.REPRODUCE)
    },
    f"POST {ORGS}/fixes/{{fix_id}}/approval": {"fix_id": _fix(FixKind.FIX, *_AWAITING)},
    **{
        f"{verb} {ORGS}/changes/{{fix_id}}{tail}": {"fix_id": _fix(FixKind.CHANGE, *_AWAITING)}
        for verb, tail in [
            ("PATCH", "/plan"),
            ("GET", ""),
            ("GET", "/events"),
            ("POST", "/cancel"),
            ("POST", "/approval"),
            ("GET", "/approvals"),
        ]
    },
}

# Bodies the schema alone can't make into one the endpoint takes as far as its role check.
BODIES: dict[str, Any] = {
    f"POST {ORGS}/api-keys": {"name": "ci", "scopes": ["read:repo"]},
    f"POST {ORGS}/invitations": {"github_login": "someone", "role": "member"},
    f"POST {REPO}/fixes": {"source": "paste", "text": "KeyError: 'id'"},
    f"POST {REPO}/changes": {"request": "Add rate limiting."},
    f"POST {ORGS}/fixes/{{fix_id}}/approval": {"decision": "approve", "plan_revision": 1},
    f"POST {ORGS}/changes/{{fix_id}}/approval": {"decision": "approve", "plan_revision": 1},
    f"PATCH {ORGS}/changes/{{fix_id}}/plan": {
        "plan_revision": 1,
        "edits": {"steps": [{"description": "Limit per key", "path": "a.py"}]},
    },
    f"POST {ORGS}/repos/import": {"url": "https://github.com/octo/hello"},
    f"POST {ORGS}/repos/estimate": {"url": "https://github.com/octo/hello"},
    f"POST {ORGS}/repos": {"github_repo_id": 1},
}
# Bodies naming the org itself (the OAuth consents pick one of the caller's orgs in the body).
ORG_BODIES = {
    f"POST {API}/oauth/requests/{{handle}}/decision": "org",
    f"POST {API}/oauth/device/decision": "org",
}


# ----------------------------------------------------------------------------- bodies


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _instance(spec: dict[str, Any], schema: dict[str, Any], depth: int = 0) -> Any:
    """A small value the schema accepts (required properties only)."""
    schema = _resolve(spec, schema)
    if depth > 6:
        return None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for combined in ("anyOf", "oneOf", "allOf"):
        if combined in schema:
            options = [o for o in schema[combined] if _resolve(spec, o).get("type") != "null"]
            return _instance(spec, (options or schema[combined])[0], depth + 1)
    if "default" in schema and schema.get("type") != "object":
        return schema["default"]
    kind = schema.get("type", "object")
    if kind == "object":
        props: dict[str, Any] = schema.get("properties", {})
        required = [name for name in schema.get("required", []) if name in props]
        return {name: _instance(spec, props[name], depth + 1) for name in required}
    if kind == "array":
        item = schema.get("items", {})
        return [_instance(spec, item, depth + 1) for _ in range(schema.get("minItems", 0))]
    if kind == "string":
        formats = {
            "uuid": str(uuid.uuid4()),
            "email": "someone@example.com",
            "uri": "https://example.com/x",
            "date-time": datetime.now(UTC).isoformat(),
        }
        return formats.get(schema.get("format", ""), "x" * max(schema.get("minLength", 1), 3))
    if kind in ("integer", "number"):
        return schema.get("minimum", schema.get("exclusiveMinimum", 0) + 1)
    if kind == "boolean":
        return False
    return None


# ----------------------------------------------------------------------------- calling


@dataclass
class Outcome:
    status: int
    code: str | None
    message: str | None
    body: bytes = b""

    def refused(self) -> bool:
        """Refused for who the caller is (not for what was asked)."""
        if self.status in (401, 403):
            return True
        not_there = self.code == "org_not_found" or (
            self.code == "not_found" and self.message == "Not found."  # require_staff's
        )
        return self.status == 404 and not_there

    def meets(self, expected: Expect) -> bool:
        if expected == OK:  # a service tests don't configure may answer 502/503, never 500
            return not self.refused() and self.status != 500
        if isinstance(expected, tuple):
            return (self.status, self.code) == expected
        return self.status == expected

    def __str__(self) -> str:
        return f"{self.status} {self.code}" if self.code else str(self.status)


async def _call(
    app: FastAPI,
    method: str,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
) -> Outcome:
    """One request straight into the ASGI app. A stream counts as answered once its response
    starts (the client is gone right after), so endpoints that stream don't hang the test."""
    raw = b"" if body is None else orjson.dumps(body)
    sent = [(b"host", b"testserver")]
    if body is not None:
        sent.append((b"content-type", b"application/json"))
    sent += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": urlencode(query or {}).encode(),
        "headers": sent,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    started, finished = asyncio.Event(), asyncio.Event()
    status = 0
    chunks: list[bytes] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            started.set()
            kind = dict(message.get("headers", [])).get(b"content-type", b"")
            if kind.startswith(b"text/event-stream"):
                finished.set()
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                finished.set()

    task = asyncio.create_task(app(scope, receive, send))
    waiting = asyncio.create_task(finished.wait())
    await asyncio.wait({task, waiting}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    if status == 0:
        return Outcome(500, "no_response", None)
    data = b"".join(chunks)
    code = message = None
    if status >= 400 and data.startswith(b"{"):
        answer = orjson.loads(data)
        error = answer.get("error", {})
        if isinstance(error, str):  # OAuth's own shape (RFC 6749, section 5.2)
            code, message = error, answer.get("error_description")
        else:
            code, message = error.get("code"), error.get("message")
    return Outcome(status, code, message, data)


@cache
def _spec() -> dict[str, Any]:
    """The OpenAPI document (the same for every app the tests make)."""
    return create_app(Settings(env="test")).openapi()


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                yield f"{method.upper()} {path}", op


_SCHEMA = object()  # the body: from BODIES or the request schema


async def _request(
    world: World,
    name: str,
    principal: str,
    params: Mapping[str, Param] | None = None,
    body: Any = _SCHEMA,
) -> Outcome:
    spec = _spec()
    method, url = name.split(" ", 1)
    op = spec["paths"][url][method.lower()]
    params = {**PARAMS, **OP_PARAMS.get(name, {}), **(params or {})}
    query: dict[str, str] = {}
    for parameter in op.get("parameters", []):
        where, key = parameter["in"], parameter["name"]
        if where == "path":
            url = url.replace("{" + key + "}", await params[key](world, principal))
        elif where == "query" and parameter.get("required"):
            make = params.get(key)
            value = await make(world, principal) if make else _instance(spec, parameter["schema"])
            query[key] = str(value)
    if body is not _SCHEMA:
        pass
    elif name in BODIES:
        body = BODIES[name]
    elif "requestBody" not in op:
        body = None
    else:
        content = op["requestBody"].get("content", {})
        schema = content.get("application/json", {}).get("schema")
        body = None if schema is None else _instance(spec, schema)
    if name in ORG_BODIES:
        body = {**body, ORG_BODIES[name]: world.org.slug}
    headers: dict[str, str] = {}
    if principal == "key":
        headers["authorization"] = f"Bearer {await _bearer(world)}"
    elif principal in world.sessions:
        token, csrf = world.sessions[principal]
        session_cookie, csrf_cookie = cookie_names(world.app.state.settings)
        headers["cookie"] = f"{session_cookie}={token}; {csrf_cookie}={csrf}"
        headers["origin"] = world.app.state.settings.web.origin
        headers["x-csrf-token"] = csrf
    return await _call(world.app, method, url, query=query, body=body, headers=headers)


# ----------------------------------------------------------------------------- fixtures


class Recorder:
    """Background work the API asks for: recorded, never sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Callable[..., Awaitable[bool]]:
        async def record(*args: Any, **kwargs: Any) -> bool:
            self.calls.append((name, args))
            return True

        return record


@pytest.fixture
async def app(
    api_app: FastAPI, fake_redis: FakeAsyncRedis, db_session: AsyncSession
) -> AsyncGenerator[FastAPI]:
    state = api_app.state
    state.redis = fake_redis
    state.org_service = OrgService(MemoryMailer(), state.settings.web.origin)
    for name in ("repo_jobs", "galaxy_jobs", "fix_jobs", "webhook_dispatcher"):
        setattr(state, name, Recorder())

    @asynccontextmanager
    async def same_session() -> AsyncGenerator[AsyncSession]:
        yield db_session  # streams' and webhooks' own sessions see the test's rows

    state.stream_sessions = same_session
    with respx.mock(assert_all_called=False) as outside:
        # GitHub says every caller may push (deciding on a fix asks it, live); every other
        # request leaving the process is refused.
        outside.get(url__regex=r"^https://api\.github\.com/repositories/\d+$").respond(
            json={"id": 1, "private": False, "permissions": {"pull": True, "push": True}}
        )
        outside.route().respond(503)
        async with httpx.AsyncClient() as http:
            state.http = http
            yield api_app


@pytest.fixture
async def world(app: FastAPI, db_session: AsyncSession, migrated_test_db: Settings) -> World:
    return await _world(app, db_session, migrated_test_db)


# ----------------------------------------------------------------------------- the tests


def test_every_operation_is_classified() -> None:
    names = {name for name, _ in _operations(_spec())}
    missing = sorted(names - set(MATRIX))
    stale = sorted((set(MATRIX) | KEY_OK | KEY_SCOPE) - names)
    assert not missing, f"operations nobody classified in MATRIX: {missing}"
    assert not stale, f"operations named here that the API no longer has: {stale}"
    for name, rule in MATRIX.items():
        assert set(rule) == set(PRINCIPALS), name


@pytest.mark.skipif(not os.environ.get("AUTHZ_RECORD"), reason="AUTHZ_RECORD=1 prints the table")
async def test_record(app: FastAPI, db_session: AsyncSession, migrated_test_db: Settings) -> None:
    lines: list[str] = []
    for name, _ in _operations(_spec()):
        world = await _world(app, db_session, migrated_test_db)
        row = [str(await _request(world, name, p)) for p in (*PRINCIPALS, "key")]
        lines.append(f"{name} | " + " | ".join(row))
    print("\n".join(lines))


@pytest.mark.parametrize("name", sorted(MATRIX))
async def test_each_caller_gets_what_the_matrix_says(world: World, name: str) -> None:
    wrong: list[str] = []
    expected: dict[str, Expect] = {**MATRIX[name], "key": key_rule(name)}
    for principal, expect in expected.items():  # least trusted first: a refusal changes nothing
        outcome = await _request(world, name, principal)
        if not outcome.meets(expect):
            wrong.append(f"{principal}: got {outcome}, expected {expect}")
    assert not wrong, f"{name}\n" + "\n".join(wrong)


# One's own conversations and answers are one's own: someone else's, even an owner's view of a
# member's, isn't there (the rows exist: the author reaches them, the control below).
SOMEONE_ELSES: dict[str, tuple[str, str]] = {
    f"GET {ORGS}/conversations/{{conversation_id}}": ("conversation", "conversation_not_found"),
    f"PATCH {ORGS}/conversations/{{conversation_id}}": ("conversation", "conversation_not_found"),
    f"DELETE {ORGS}/conversations/{{conversation_id}}": ("conversation", "conversation_not_found"),
    f"POST {ORGS}/conversations/{{conversation_id}}/share": (
        "conversation",
        "conversation_not_found",
    ),
    f"DELETE {ORGS}/conversations/{{conversation_id}}/share": (
        "conversation",
        "conversation_not_found",
    ),
    f"POST {ORGS}/messages/{{message_id}}/feedback": ("message", "message_not_found"),
    f"DELETE {ORGS}/messages/{{message_id}}/feedback": ("message", "message_not_found"),
    f"GET {REPO}/ask/messages/{{message_id}}/stream": ("message", "conversation_not_found"),
}
_PARAM_OF = {"conversation": "conversation_id", "message": "message_id"}


@pytest.mark.parametrize("name", sorted(SOMEONE_ELSES))
async def test_someone_elses_conversation_is_not_there(world: World, name: str) -> None:
    what, code = SOMEONE_ELSES[name]
    theirs = {_PARAM_OF[what]: _authors(what)}
    for principal in (*ROLES, "key"):  # a key that may write here at all: the owner's
        refusal = key_rule(name) if principal == "key" else OK
        expected = (404, code) if refusal == OK else refusal
        outcome = await _request(world, name, principal, theirs)
        assert outcome.meets(expected), (principal, str(outcome))
    mine = await _request(world, name, "author", theirs)  # the author's own: there
    assert not mine.refused(), str(mine)
    assert mine.code != code, str(mine)


def _as(user_id: Param) -> dict[str, Param]:
    return {"user_id": user_id}


def _user(principal: str) -> Param:
    async def get(world: World, _caller: str) -> str:
        return str(world.users[principal].id)

    return get


# Nobody raises themselves or anyone else above their own role, or touches an owner without
# being one: (caller, operation, path parameters, body) -> the refusal.
CLIMBS: list[tuple[str, str, dict[str, Param], Any, Expect]] = [
    ("admin", f"PATCH {ORGS}/members/{{user_id}}", {}, {"role": "owner"}, 403),
    ("admin", f"PATCH {ORGS}/members/{{user_id}}", _as(_user("admin")), {"role": "owner"}, 403),
    ("admin", f"PATCH {ORGS}/members/{{user_id}}", _as(_user("owner")), {"role": "member"}, 403),
    ("admin", f"DELETE {ORGS}/members/{{user_id}}", _as(_user("owner")), None, 403),
    ("admin", f"POST {ORGS}/invitations", {}, {"github_login": "x", "role": "owner"}, 403),
    ("member", f"PATCH {ORGS}/members/{{user_id}}", _as(_user("member")), {"role": "admin"}, 403),
    ("viewer", f"PATCH {ORGS}/members/{{user_id}}", _as(_user("viewer")), {"role": "member"}, 403),
    ("member", f"POST {ORGS}/invitations", {}, {"github_login": "x", "role": "viewer"}, 403),
    # An invitation addressed to someone else can't be taken up.
    (
        "outsider",
        f"POST {API}/invitations/{{token}}/accept",
        {"token": lambda w, _p: _invite_token(w, "author")},
        None,
        (403, "invitation_wrong_account"),
    ),
    (
        "viewer",
        f"POST {API}/invitations/{{token}}/accept",
        {"token": lambda w, _p: _invite_token(w, "author")},
        None,
        (403, "invitation_wrong_account"),
    ),
]


@pytest.mark.parametrize(
    ("caller", "name", "params", "body", "expected"),
    CLIMBS,
    ids=[f"{c[0]}-{c[1].split(' ')[0]}-{i}" for i, c in enumerate(CLIMBS)],
)
async def test_no_one_climbs_above_their_role(
    world: World, caller: str, name: str, params: dict[str, Param], body: Any, expected: Expect
) -> None:
    outcome = await _request(world, name, caller, params, body=_SCHEMA if body is None else body)
    assert outcome.meets(expected), str(outcome)


async def test_an_owner_may_make_another_owner(world: World) -> None:
    """The control for the refusals above: the same request from an owner is let through."""
    name = f"PATCH {ORGS}/members/{{user_id}}"
    outcome = await _request(world, name, "owner", _as(_user("admin")), body={"role": "owner"})
    assert outcome.status == 200, str(outcome)


async def test_insights_show_costs_to_admins_only(world: World) -> None:
    name = f"GET {ORGS}/insights"
    for principal, role in ROLES.items():
        outcome = await _request(world, name, principal)
        assert outcome.status == 200, (principal, str(outcome))
        body = orjson.loads(outcome.body)
        admin = role.at_least(Role.ADMIN)
        assert body["costs"] is admin, principal
        assert all((day["cost_usd"] is not None) is admin for day in body["days"]), principal


async def test_run_costs_are_shown_to_admins_only(world: World) -> None:
    await set_org_context(world.db, world.org_id)
    await _talk(world, "author")
    run = await world.db.get(AgentRun, world.own[("author", "run")])
    assert run is not None
    run.cost_usd = Decimal("1.25")
    await world.db.flush()
    for name in (f"GET {REPO}/runs", f"GET {ORGS}/agent-runs/{{run_id}}"):
        outcome = await _request(world, name, "viewer", {"run_id": _authors("run")})
        assert outcome.status == 200, str(outcome)
        assert b"1.25" not in outcome.body, name
        shown = await _request(world, name, "admin", {"run_id": _authors("run")})
        assert b"1.25" in shown.body, name
