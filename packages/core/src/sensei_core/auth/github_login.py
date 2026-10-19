"""Turn a completed GitHub authorization into a repo-sensei user, and keep their token fresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.github.oauth import GitHubOAuth, GitHubTokens, GitHubUser, OAuthError
from sensei_core.models import OAuthToken, User
from sensei_core.security.crypto import TokenCipher

PROVIDER = "github"
REFRESH_MARGIN = timedelta(minutes=5)


async def upsert_user(db: AsyncSession, profile: GitHubUser) -> User:
    """Create the user on first sign-in; refresh profile fields on later ones."""
    user = await db.scalar(select(User).where(User.github_id == profile.id))
    if user is None:
        user = User(github_id=profile.id, login=profile.login)
        db.add(user)
    elif user.deleted_at is not None:
        raise OAuthError("account_deleted", "This account has been deleted.")
    user.login = profile.login
    user.name = profile.name
    user.email = profile.email
    user.avatar_url = profile.avatar_url
    await db.flush()
    return user


async def store_tokens(
    db: AsyncSession, cipher: TokenCipher, user: User, tokens: GitHubTokens
) -> OAuthToken:
    """Encrypt and store the user's GitHub tokens (user id bound as associated data)."""
    context = str(user.id)
    row = await db.scalar(
        select(OAuthToken).where(OAuthToken.user_id == user.id, OAuthToken.provider == PROVIDER)
    )
    if row is None:
        row = OAuthToken(user_id=user.id, provider=PROVIDER)
        db.add(row)
    row.access_token_enc = cipher.encrypt_text(tokens.access_token, context=context)
    row.refresh_token_enc = (
        cipher.encrypt_text(tokens.refresh_token, context=context) if tokens.refresh_token else None
    )
    row.expires_at = tokens.expires_at
    row.refresh_expires_at = tokens.refresh_expires_at
    row.scopes = tokens.scopes
    await db.flush()
    return row


async def complete_login(
    db: AsyncSession, cipher: TokenCipher, oauth: GitHubOAuth, tokens: GitHubTokens
) -> User:
    profile = await oauth.fetch_user(tokens.access_token)
    user = await upsert_user(db, profile)
    await store_tokens(db, cipher, user, tokens)
    return user


async def fresh_access_token(
    db: AsyncSession,
    cipher: TokenCipher,
    oauth: GitHubOAuth,
    user: User,
    *,
    now: datetime | None = None,
) -> str:
    """The user's GitHub access token, refreshed first if it expires within five minutes."""
    now = now or datetime.now(UTC)
    context = str(user.id)
    row = await db.scalar(
        select(OAuthToken).where(OAuthToken.user_id == user.id, OAuthToken.provider == PROVIDER)
    )
    if row is None:
        raise OAuthError("github_reauth_required", "Please sign in with GitHub again.")
    if row.expires_at is None or row.expires_at - REFRESH_MARGIN > now:
        return cipher.decrypt_text(row.access_token_enc, context=context)
    refresh_valid = row.refresh_token_enc is not None and (
        row.refresh_expires_at is None or row.refresh_expires_at > now
    )
    if not refresh_valid or row.refresh_token_enc is None:
        raise OAuthError("github_reauth_required", "Please sign in with GitHub again.")
    tokens = await oauth.refresh(cipher.decrypt_text(row.refresh_token_enc, context=context))
    await store_tokens(db, cipher, user, tokens)
    return tokens.access_token
