"""Cloudflare Turnstile (10.2): that a question asked by someone not signed in came from a
person's browser. The page's widget gives a token (single-use, five minutes); the server asks
Cloudflare whether it is good, with its secret and the asker's address.

Refused when Cloudflare says no, and when it can't be asked (a check that can't be made lets
nothing through: it guards spend).
"""

from __future__ import annotations

import uuid
from typing import cast

import httpx
from pydantic import SecretStr

from sensei_core.errors import DomainError
from sensei_core.logging import get_logger

log = get_logger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_TOKEN_CHARS = 2_048  # Cloudflare's
TIMEOUT_SECONDS = 5.0
ACTION = "ask"  # what the page's widget says it is for
# Cloudflare's test secrets (pass, fail, already spent): they answer for any page.
TEST_SECRETS = frozenset(f"{n}x0000000000000000000000000000000AA" for n in (1, 2, 3))


class HumanCheckFailedError(DomainError):
    status_code = 403
    code = "human_check_failed"


class HumanCheckUnavailableError(DomainError):
    status_code = 503
    code = "human_check_unavailable"


async def verify(
    http: httpx.AsyncClient,
    secret: SecretStr,
    token: str,
    address: str | None,
    *,
    hostname: str | None = None,
) -> None:
    """:class:`HumanCheckFailedError` unless Cloudflare says ``token`` is good, for the page's
    question (``ACTION``) on ``hostname`` (the site's: a token from a page elsewhere that uses
    the same key isn't); :class:`HumanCheckUnavailableError` when it can't be asked (or there is
    no secret). Cloudflare's test secrets pass any host and action."""
    require(secret)
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise _failed()
    form = {
        "secret": secret.get_secret_value(),
        "response": token,
        # Asked again on a timeout, Cloudflare answers the first try's verdict.
        "idempotency_key": str(uuid.uuid4()),
    }
    if address:
        form["remoteip"] = address
    for _ in range(2):
        try:
            response = await http.post(VERIFY_URL, data=form, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            body: object = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("turnstile unavailable", error=type(exc).__name__)
            continue
        said = cast("dict[str, object]", body) if isinstance(body, dict) else {}
        if said.get("success") is not True:
            log.info("turnstile refused", codes=said.get("error-codes"))
            raise _failed()
        if secret.get_secret_value() in TEST_SECRETS or _ours(said, hostname):
            return
        log.info("turnstile elsewhere", hostname=said.get("hostname"), action=said.get("action"))
        raise _failed()
    raise HumanCheckUnavailableError("The check that you're a person can't be made right now.")


def _ours(said: dict[str, object], hostname: str | None) -> bool:
    if said.get("action") != ACTION:
        return False
    return hostname is None or str(said.get("hostname") or "").lower() == hostname.lower()


def require(secret: SecretStr) -> None:
    """:class:`HumanCheckUnavailableError` without a secret: no check, so no questions."""
    if not secret.get_secret_value():
        raise HumanCheckUnavailableError("Questions aren't open on public pages here.")


def _failed() -> HumanCheckFailedError:
    return HumanCheckFailedError("The check that you're a person didn't pass. Try again.")
