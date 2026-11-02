"""The repo-sensei API as the CLI calls it, signed in one of two ways:

* the user's sign-in (``sensei login``): an OAuth access token for the API, refreshed when it
  is about to expire or is refused (once), the refresh token rotating;
* an API key in ``SENSEI_API_KEY`` (for scripts and CI): sent as it is.

API errors become :class:`CliError`, with what to do when the answer says (sign in again, or
sign in with a scope the command needs).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Generator, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import httpx

from sensei_cli.credentials import Credentials, Tokens, refresh_lock
from sensei_cli.errors import CliError

API = "/api/v1"
CLIENT_ID = "sensei-cli"
TIMEOUT = httpx.Timeout(30.0, read=180.0)  # an answer can take a while
USER_AGENT = "sensei-cli/0.1"
SIGN_IN = "Run `sensei login`."
# What to run for a scope a command needs and the sign-in lacks.
SCOPE_HINTS = {
    "connect:repo": "Run `sensei login --connect` to sign in with it (org admins).",
    "ask": SIGN_IN,
    "read:repo": SIGN_IN,
}

Json = dict[str, Any]


def signed_out() -> CliError:
    return CliError("You're not signed in.", hint=SIGN_IN, code="signed_out")


class Session:
    """The user's tokens for one server: the access token, fresh, refreshed when needed."""

    def __init__(
        self,
        credentials: Credentials,
        lock_dir: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credentials = credentials
        self._lock_dir = lock_dir
        self._transport = transport
        self._clock = clock
        self._tokens: Tokens | None = None

    @property
    def tokens(self) -> Tokens:
        if self._tokens is None:
            self._tokens = self._credentials.load()
        if self._tokens is None:
            raise signed_out()
        return self._tokens

    def access(self) -> str:
        tokens = self.tokens
        if tokens.fresh(self._clock()):
            return tokens.access_token
        return self.refresh(tokens.access_token)

    def refresh(self, stale: str) -> str:
        """A new access token for ``stale`` (unless another command already got one)."""
        with refresh_lock(self._lock_dir):
            kept = self._credentials.load()
            if kept is None:
                raise signed_out()
            if kept.access_token != stale and kept.fresh(self._clock()):
                self._tokens = kept  # another command refreshed meanwhile
                return kept.access_token
            form = {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": kept.refresh_token,
            }
            if kept.resource:
                form["resource"] = kept.resource
            with httpx.Client(transport=self._transport, timeout=TIMEOUT) as http:
                answer = http.post(kept.token_endpoint, data=form)
            if answer.status_code in (400, 401):
                self._credentials.delete()
                self._tokens = None
                raise CliError("Your sign-in has expired.", hint=SIGN_IN, code="signed_out")
            if answer.status_code >= 400:
                raise CliError(f"Couldn't refresh your sign-in ({answer.status_code}).")
            self._tokens = issued(answer.json(), kept, now=self._clock())
            self._credentials.save(self._tokens)
            return self._tokens.access_token


def issued(body: Json, like: Tokens, *, now: float) -> Tokens:
    """Tokens from a token endpoint's answer (``like``: where they came from)."""
    scope = body.get("scope")
    return Tokens(
        access_token=str(body["access_token"]),
        refresh_token=str(body.get("refresh_token") or like.refresh_token),
        expires_at=now + float(body.get("expires_in") or 3600),
        scopes=str(scope).split() if scope else like.scopes,
        token_endpoint=like.token_endpoint,
        revocation_endpoint=like.revocation_endpoint,
        resource=like.resource,
    )


class TokenAuth(httpx.Auth):
    """The access token on each request; refused once (revoked, or expired early): refreshed and
    sent again."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        token = self._session.access()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._session.refresh(token)}"
            yield request


class BearerAuth(httpx.Auth):
    """A credential sent as it is: an API key (or a token just issued)."""

    def __init__(self, key: str) -> None:
        self._key = key

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {self._key}"
        yield request


class Api:
    """The API, signed in. Paths are under ``/api/v1``; ``org`` is the sign-in's."""

    def __init__(
        self, server: str, auth: httpx.Auth, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.server = server
        self._http = httpx.Client(
            base_url=server + API,
            auth=auth,
            transport=transport,
            timeout=TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        self._org: str | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Api:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def me(self) -> Json:
        return self.get("/me")

    def org(self) -> str:
        """The org the sign-in or key is for (it is for one)."""
        if self._org is None:
            orgs = cast("list[Json]", self.me().get("orgs") or [])
            if len(orgs) != 1:
                raise CliError(
                    "This sign-in isn't for one organization.", hint=SIGN_IN, code="no_org"
                )
            self._org = str(orgs[0]["slug"])
        return self._org

    def repo_path(self, repo: str, rest: str = "") -> str:
        return f"/orgs/{self.org()}/repos/{repo}{rest}"

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Json:
        return cast("Json", self._send("GET", path, params=params))

    def get_list(self, path: str) -> list[Json]:
        return cast("list[Json]", self._send("GET", path))

    def post(self, path: str, body: Mapping[str, Any]) -> Json:
        return cast("Json", self._send("POST", path, json=body))

    def _send(self, method: str, path: str, **kw: Any) -> Any:
        try:
            response = self._http.request(method, path, **kw)
        except httpx.TransportError as error:
            raise unreachable(self.server) from error
        raise_for(response)
        return response.json()

    def stream(self, path: str, body: Mapping[str, Any]) -> Iterator[Json]:
        """A server-sent event stream's events (each ``data:`` line's JSON)."""
        try:
            with self._http.stream(
                "POST", path, json=body, headers={"Accept": "text/event-stream"}
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise_for(response)
                for line in response.iter_lines():
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
                        yield cast("Json", event)
        except httpx.TransportError as error:
            raise unreachable(self.server) from error


def unreachable(server: str) -> CliError:
    return CliError(
        f"Couldn't reach {server}.",
        hint="Check the address, or sign in to another server with `sensei login --server`.",
        code="unreachable",
    )


def _required_scope(response: httpx.Response, details: Json) -> str | None:
    if isinstance(found := details.get("required_scope"), str):
        return found
    challenge = response.headers.get("www-authenticate", "")
    marker = 'scope="'
    if "insufficient_scope" in challenge and marker in challenge:
        return challenge.split(marker, 1)[1].split('"', 1)[0]
    return None


def raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    code, message, details = "http_error", f"The server answered {response.status_code}.", {}
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = cast("Json", body).get("error")
        if isinstance(error, dict):
            found = cast("Json", error)
            code = str(found.get("code") or code)
            message = str(found.get("message") or message)
            details = cast("Json", found.get("details") or {})
    hint = None
    if response.status_code == 401:
        hint = SIGN_IN
    elif (scope := _required_scope(response, details)) is not None:
        hint = SCOPE_HINTS.get(scope, SIGN_IN)
    raise CliError(message, hint=hint, code=code)
