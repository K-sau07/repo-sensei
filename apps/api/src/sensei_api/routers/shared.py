"""``GET /s/{token}`` (4.11): a shared conversation, read-only, signed in or not.

The token's hash finds the one conversation (row-level security lets a transaction read the
conversation whose hash it presents, and no other). Its citations are shown to a viewer who can
read its repository: anyone, for a public repository imported by its URL; else a member of the
org whom GitHub lets read it. Anyone else sees the answers with their citations redacted
(:mod:`sensei_api.sharing`). An unknown or reset link, or one whose org is gone, is not found.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.auth import cookie_names, get_session_service
from sensei_api.deps import DbSession
from sensei_api.repos import repo_access
from sensei_api.routers.ask import MessageOut, citations_of, message_out
from sensei_api.routers.auth import GitHubOAuthDep
from sensei_api.sharing import share_token_hash, shown
from sensei_core.db.tenancy import set_org_context, set_share_context, set_user_context
from sensei_core.errors import NotFoundError
from sensei_core.github.client import GitHubError
from sensei_core.github.oauth import GitHubOAuth, OAuthError
from sensei_core.models import Conversation, ConversationMessage, Membership, Org, Repo, User
from sensei_core.repos.access import open_to_all

log = structlog.get_logger(__name__)

router = APIRouter(tags=["ask"])

MAX_TOKEN_CHARS = 128


class SharedConversation(BaseModel):
    title: str
    repo: str | None  # "owner/name", for a viewer who can read it
    redacted: bool  # its citations are hidden from this viewer
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]


@router.get("/s/{token}", response_model=SharedConversation)
async def shared(
    *, request: Request, db: DbSession, oauth: GitHubOAuthDep, token: str
) -> SharedConversation:
    """A shared conversation: its questions and finished answers, read-only."""
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise _gone()
    token_hash = share_token_hash(token)
    await set_share_context(db, token_hash)
    found = await db.scalar(select(Conversation).where(Conversation.share_token_hash == token_hash))
    if found is None:
        raise _gone()
    org = await db.get(Org, found.org_id)
    if org is None or org.deleted_at is not None:
        raise _gone()
    viewer = await _viewer(request, db)
    await set_org_context(db, found.org_id)
    repo = await db.get(Repo, found.repo_id)
    if repo is None:
        raise _gone()
    readable = await _can_read(request, db, oauth, viewer, repo)
    messages = [
        m
        for m in await db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == found.id)
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
        )
        if m.role == "user" or m.status == "done"
    ]
    cited = await citations_of(db, messages) if readable else {}
    return SharedConversation(
        title=found.title,
        repo=f"{repo.owner}/{repo.name}" if readable else None,
        redacted=not readable,
        created_at=found.created_at,
        updated_at=found.updated_at,
        messages=[
            message_out(m, shown(m.parts, redact=not readable), cited.get(m.id, []))
            for m in messages
        ],
    )


async def _viewer(request: Request, db: AsyncSession) -> User | None:
    """Who is looking, if signed in. A lapsed session is no one (the page is public)."""
    token = request.cookies.get(cookie_names(request.app.state.settings)[0])
    if not token:
        return None
    active = await get_session_service(request).authenticate(db, token)
    return active.user if active is not None else None


async def _can_read(
    request: Request,
    db: AsyncSession,
    oauth: GitHubOAuth | None,
    viewer: User | None,
    repo: Repo,
) -> bool:
    if open_to_all(repo):
        return True
    if viewer is None or not await _member(db, viewer.id, repo.org_id):
        return False
    try:
        return bool(await repo_access(request, db, oauth).readable(viewer, [repo]))
    except (OAuthError, GitHubError) as exc:  # can't tell: shown as to anyone
        log.info("shared.access_unknown", repo_id=str(repo.id), error=type(exc).__name__)
        return False


async def _member(db: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    await set_user_context(db, user_id)
    found = await db.scalar(
        select(Membership.user_id).where(Membership.user_id == user_id, Membership.org_id == org_id)
    )
    return found is not None


def _gone() -> NotFoundError:
    return NotFoundError("This link doesn't open anything.", code="share_not_found")
