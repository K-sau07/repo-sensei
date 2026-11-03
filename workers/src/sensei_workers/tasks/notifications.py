"""Notifications' emails and Slack posts (8.10): what's pending, sent, every minute (beat). See
:mod:`sensei_core.notifications.deliver`."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.auth.github_login import fresh_access_token
from sensei_core.github.oauth import GitHubOAuth, OAuthError
from sensei_core.jobs import TaskName
from sensei_core.models import Repo, User
from sensei_core.notifications.deliver import CanRead, Senders, deliver_pending
from sensei_core.notifications.senders import HttpSlackPoster, emailer
from sensei_core.repos.access import RepoAccess
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import Runtime, get_runtime


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.DELIVER_NOTIFICATIONS.value, system=True)
def deliver_notifications(self: SenseiTask) -> int:
    """Send what's pending (every org's: the system database). Returns how many were sent."""
    runtime = get_runtime()
    settings = runtime.settings
    http = runtime.notify_http
    senders = Senders(
        emailer=emailer(http, settings),
        slack=HttpSlackPoster(http),
        cipher=runtime.cipher,
        origin=settings.web.origin,
        max_attempts=settings.notify.max_attempts,
        can_read=can_read(runtime, self.system_session),
    )
    done = runtime.run(deliver_pending(self.system_session, senders))
    return done.sent


def can_read(
    runtime: Runtime, sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]]
) -> CanRead | None:
    """Whether a person may read a repository, asked of GitHub with their own token (as the API
    does, sharing its cached answers). None without GitHub sign-in configured."""
    settings = runtime.settings
    if not settings.github.oauth_configured:
        return None
    # Only refreshing tokens: no sign-in starts here, so the redirect is never used.
    oauth = GitHubOAuth(settings.github, runtime.redis, runtime.notify_http, settings.web.origin)

    async def token_for(user: User) -> str:
        # Its own transaction: a refreshed token is kept even if the delivery is rolled back
        # (GitHub has already retired the old refresh token).
        async with sessions() as db:
            return await fresh_access_token(db, runtime.cipher, oauth, user)

    access = RepoAccess(runtime.redis, runtime.github_client, token_for)

    async def check(user: User, repo: Repo) -> bool | None:
        try:
            return await access.can_read(user, repo)
        except OAuthError:
            return None

    return check
