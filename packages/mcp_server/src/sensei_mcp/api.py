"""The repo-sensei API, as the MCP tools call it.

Every tool is a call (or two) to the REST API with the caller's credential, so what a tool can
reach is what that credential can: its org, its scopes, the user's repositories. Locally (stdio)
that is an API key against the hosted API; mounted in the API (Streamable HTTP), the caller's
OAuth token, sent to the same app in-process.

API errors become :class:`ApiError` (status, code, message) for the tools to report.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast
from urllib.parse import quote

import httpx

API = "/api/v1"
TIMEOUT = httpx.Timeout(30.0, read=120.0)  # asking can take a while


SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def segment(slug: str) -> str:
    """``slug`` as one path segment: a slug (an org's, a repository's) or nothing. A tool's
    arguments are the model's, so ``../`` or ``?`` must never steer a request elsewhere."""
    if not SLUG.match(slug) or slug in (".", ".."):
        raise ApiError(400, "invalid_slug", f"{slug!r} isn't a repository or organization name.")
    return quote(slug, safe="")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class SenseiApi:
    """One caller's view of the API (one credential: one org)."""

    def __init__(
        self,
        base_url: str,
        authorization: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": authorization, "Accept": "application/json"},
            transport=transport,
            timeout=TIMEOUT,
        )
        self._org: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SenseiApi:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def org(self) -> str:
        """The org the credential is for (an API key or token is for one)."""
        if self._org is None:
            me = await self.get("/me")
            orgs = cast("list[dict[str, Any]]", me.get("orgs") or [])
            if len(orgs) != 1:
                raise ApiError(
                    403, "no_org", "This credential isn't for one organization: use an API key "
                    "or sign in again from your editor.",
                )  # fmt: skip
            self._org = str(orgs[0]["slug"])
        return self._org

    async def repo_path(self, repo: str, rest: str = "") -> str:
        return f"/orgs/{segment(await self.org())}/repos/{segment(repo)}{rest}"

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self._json("GET", path, params=params)

    async def get_list(self, path: str) -> list[dict[str, Any]]:
        response = await self._client.get(API + path)
        _raise_for(response)
        return cast("list[dict[str, Any]]", response.json())

    async def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._json("POST", path, json=body)

    async def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        response = await self._client.request(method, API + path, **kw)
        _raise_for(response)
        return cast("dict[str, Any]", response.json())

    async def stream(self, path: str, body: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """A server-sent event stream's events (each ``data:`` line's JSON)."""
        async with self._client.stream(
            "POST", API + path, json=body, headers={"Accept": "text/event-stream"}
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                _raise_for(response)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield cast("dict[str, Any]", event)


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    code, message = "http_error", f"The API answered {response.status_code}."
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = cast("dict[str, Any]", body).get("error")
        if isinstance(error, dict):
            found = cast("dict[str, Any]", error)
            code = str(found.get("code") or code)
            message = str(found.get("message") or message)
    raise ApiError(response.status_code, code, message)
