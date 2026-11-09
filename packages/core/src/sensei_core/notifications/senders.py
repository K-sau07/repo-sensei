"""Sending notifications beyond the inbox (8.10): email through Resend, and a post to an org's
Slack incoming webhook.

Without a Resend key, development logs each email (:class:`ConsoleEmailer`) and other
environments have no emailer (those deliveries are skipped). A Slack webhook URL must be one of
Slack's own (``https://hooks.slack.com/services/...``): the server posts to it, so any other
address would let an admin make it call wherever they liked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from sensei_core.config import Settings
from sensei_core.logging import get_logger

log = get_logger(__name__)

SLACK_WEBHOOK = re.compile(
    r"https://hooks\.slack\.com/services/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+){1,3}"
)
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class SendError(Exception):
    """Not sent; ``retry`` says whether trying again could help (a 5xx, a timeout) or not (a
    4xx: the address or the webhook is wrong)."""

    def __init__(self, message: str, *, retry: bool) -> None:
        super().__init__(message)
        self.retry = retry


@dataclass(frozen=True, slots=True)
class Email:
    to: str
    subject: str
    text: str


class Emailer(Protocol):
    async def send(self, email: Email, *, key: str) -> None: ...


class ResendEmailer:
    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        key = settings.notify.resend_api_key
        if key is None:
            msg = "ResendEmailer needs SENSEI_NOTIFY_RESEND_API_KEY"
            raise ValueError(msg)
        self._http = http
        self._key = key
        self._url = settings.notify.resend_url
        self._sender = settings.notify.email_from

    async def send(self, email: Email, *, key: str) -> None:
        body = {
            "from": self._sender,
            "to": [email.to],
            "subject": email.subject,
            "text": email.text,
        }
        # Resend keeps an idempotency key a day: a retry after a lost response isn't sent twice.
        headers = {
            "Authorization": f"Bearer {self._key.get_secret_value()}",
            "Idempotency-Key": key[:256],
        }
        await _post(self._http, self._url, json=body, headers=headers, what="email")


class ConsoleEmailer:
    """Development only: logs the email instead of sending it."""

    async def send(self, email: Email, *, key: str) -> None:
        log.info("notification email (dev console)", to=email.to, subject=email.subject,
                 text=email.text, key=key)  # fmt: skip


@dataclass
class MemoryEmailer:
    """Test double: records what it was given (``fail``: raise this instead)."""

    sent: list[Email] = field(default_factory=list[Email])
    keys: list[str] = field(default_factory=list[str])
    fail: SendError | None = None

    async def send(self, email: Email, *, key: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append(email)
        self.keys.append(key)


def emailer(http: httpx.AsyncClient, settings: Settings) -> Emailer | None:
    """The emailer the settings call for (None: email is skipped)."""
    if settings.notify.resend_api_key is not None:
        return ResendEmailer(http, settings)
    return ConsoleEmailer() if settings.is_dev else None


def valid_slack_webhook(url: str) -> bool:
    return SLACK_WEBHOOK.fullmatch(url) is not None


class SlackPoster(Protocol):
    async def post(self, webhook_url: str, text: str) -> None: ...


class HttpSlackPoster:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def post(self, webhook_url: str, text: str) -> None:
        if not valid_slack_webhook(webhook_url):
            raise SendError("not a Slack incoming webhook", retry=False)
        # Slack's mrkdwn: links in <url|text>; &, <, > escaped in what people wrote.
        await _post(self._http, webhook_url, json={"text": text}, headers={}, what="slack",
                    follow=False)  # fmt: skip


@dataclass
class MemorySlackPoster:
    posted: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    fail: SendError | None = None

    async def post(self, webhook_url: str, text: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.posted.append((webhook_url, text))


def slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _post(
    http: httpx.AsyncClient,
    url: str,
    *,
    json: object,
    headers: dict[str, str],
    what: str,
    follow: bool = True,
) -> None:
    try:
        response = await http.post(url, json=json, headers=headers, timeout=TIMEOUT,
                                   follow_redirects=follow)  # fmt: skip
    except httpx.HTTPError as exc:
        raise SendError(f"{what}: {type(exc).__name__}", retry=True) from exc
    if response.is_success:
        return
    retry = response.status_code >= 500 or response.status_code == 429
    raise SendError(f"{what}: HTTP {response.status_code}", retry=retry)
