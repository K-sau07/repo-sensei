"""A small GitHub REST client: retries, rate limits, conditional requests and pagination.

* Retries network errors and 5xx with exponential backoff and jitter.
* Rate limits (primary: `x-ratelimit-remaining: 0`; secondary: `retry-after`): waits when the
  reset is near (``rate_limit_max_wait_seconds``), otherwise raises :class:`RateLimited` so the
  caller (usually a task) can reschedule.
* GETs are conditional when an ETag cache is given: a 304 costs no rate limit and returns the
  cached body. Cached bodies are encrypted, and keyed per credential so one installation's data
  is never served to another.
* :meth:`paginate` follows `Link: rel="next"`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast

import httpx

from sensei_core.config import GitHubSettings

API_VERSION = "2022-11-28"
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class RateLimited(GitHubError):
    """Out of requests until ``reset_at``; reschedule rather than retry now."""

    def __init__(self, reset_at: datetime) -> None:
        super().__init__(429, f"rate limited until {reset_at.isoformat()}")
        self.reset_at = reset_at


class ETagCache(Protocol):
    async def get(self, key: str) -> tuple[str, Any] | None: ...
    async def set(self, key: str, etag: str, body: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class Credential:
    """The Authorization header value; ``scope`` names who it is for (keys the ETag cache)."""

    header: str
    scope: str

    @classmethod
    def anonymous(cls) -> Credential:
        """No Authorization header: public data only (60 requests an hour per IP)."""
        return cls("", "anonymous")


Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GitHubClient:
    def __init__(
        self,
        settings: GitHubSettings,
        http: httpx.AsyncClient,
        *,
        etags: ETagCache | None = None,
        sleep: Sleep = asyncio.sleep,
        now: Now = _utcnow,
    ) -> None:
        self._settings = settings
        self._http = http
        self._etags = etags
        self._sleep = sleep
        self._now = now

    @property
    def web_url(self) -> str:
        return self._settings.web_url

    async def request(
        self,
        method: str,
        path: str,
        credential: Credential,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._settings.api_url}{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        if credential.header:
            headers["Authorization"] = credential.header
        cache_key = None
        cached: tuple[str, Any] | None = None
        if method == "GET" and self._etags is not None:
            cache_key = self._cache_key(credential, url, params)
            cached = await self._etags.get(cache_key)
            if cached:
                headers["If-None-Match"] = cached[0]

        attempt = 0
        limited = 0
        while True:
            attempt += 1
            try:
                response = await self._http.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self._settings.timeout_seconds,
                )
            except httpx.TransportError:
                if attempt > self._settings.max_retries:
                    raise
                await self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 304 and cached is not None:
                return _from_cache(cached[1], response.request)
            wait = self._rate_limit_wait(response)
            if wait is not None:
                limited += 1
                if limited > self._settings.max_retries:  # still limited: give the caller a date
                    raise RateLimited(self._reset_at(wait))
                await self._sleep(wait)
                continue
            if response.status_code >= 500 and attempt <= self._settings.max_retries:
                await self._sleep(self._backoff(attempt))
                continue
            if response.status_code >= 400:
                raise GitHubError(response.status_code, _message(response))
            if cache_key and self._etags is not None and (etag := response.headers.get("etag")):
                # The Link header too: a page served from the cache must still lead to the next.
                entry = {"body": response.json(), "link": response.headers.get("link")}
                await self._etags.set(cache_key, etag, entry)
            return response

    async def get_json(self, path: str, credential: Credential, **params: Any) -> Any:
        return (await self.request("GET", path, credential, params=params or None)).json()

    async def paginate(
        self, path: str, credential: Credential, *, item_key: str | None = None, per_page: int = 100
    ) -> AsyncIterator[Any]:
        """Every item across pages; ``item_key`` for responses that wrap the list."""
        url: str | None = path
        params: dict[str, Any] | None = {"per_page": per_page}
        while url:
            response = await self.request("GET", url, credential, params=params)
            body = response.json()
            for item in body[item_key] if item_key else body:
                yield item
            match = _NEXT_LINK.search(response.headers.get("link", ""))
            url, params = (match.group(1), None) if match else (None, None)

    # ------------------------------------------------------------------ internals

    def _rate_limit_wait(self, response: httpx.Response) -> float | None:
        """Seconds to wait before retrying, None if not rate limited; raises if too long."""
        if response.status_code not in (403, 429):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            wait = self._retry_after_seconds(retry_after)
        elif response.headers.get("x-ratelimit-remaining") == "0":
            reset = float(response.headers.get("x-ratelimit-reset", "0"))
            wait = max(0.0, reset - self._now().timestamp()) + 1
        else:
            return None  # a plain 403 (permissions), not a rate limit
        if wait > self._settings.rate_limit_max_wait_seconds:
            raise RateLimited(self._reset_at(wait))
        return wait

    def _reset_at(self, wait: float) -> datetime:
        return datetime.fromtimestamp(self._now().timestamp() + wait, UTC)

    def _retry_after_seconds(self, value: str) -> float:
        """``Retry-After`` is seconds, or (per HTTP) a date."""
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 60.0  # unreadable: wait a minute
        return max(0.0, (when - self._now()).total_seconds())

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(30.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random())  # noqa: S311 - jitter

    @staticmethod
    def _cache_key(credential: Credential, url: str, params: dict[str, Any] | None) -> str:
        raw = json.dumps([credential.scope, url, sorted((params or {}).items())], default=str)
        return hashlib.sha256(raw.encode()).hexdigest()


def _from_cache(entry: Any, request: httpx.Request) -> httpx.Response:
    cached = as_object(entry) or {}
    link = cached.get("link")
    headers = {"link": link} if isinstance(link, str) else {}
    return httpx.Response(200, json=cached.get("body"), headers=headers, request=request)


def as_object(value: object) -> dict[str, Any] | None:
    """``value`` if it is a JSON object (GitHub bodies are untyped)."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _message(response: httpx.Response) -> str:
    try:
        body = as_object(response.json())
    except ValueError:
        return response.text[:200]
    return str(body.get("message", ""))[:200] if body else ""
