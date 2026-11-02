"""The CLI's tests' stand-ins (7.4): a server answering as the API does, in-process; a keyring
kept in memory; and the command line run in-process, its output compared to snapshots
(``SNAPSHOT_UPDATE=1`` writes them)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from typer.testing import CliRunner, Result

from sensei_cli import main

SERVER = "https://sensei.test"
Json = dict[str, Any]
Handler = Callable[[httpx.Request], httpx.Response | None]


class MemoryKeyring:
    def __init__(self) -> None:
        self.kept: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str, /) -> str | None:
        return self.kept.get((service, username))

    def set_password(self, service: str, username: str, password: str, /) -> None:
        self.kept[(service, username)] = password

    def delete_password(self, service: str, username: str, /) -> None:
        from keyring.errors import PasswordDeleteError  # noqa: PLC0415

        if self.kept.pop((service, username), None) is None:
            raise PasswordDeleteError("none")


def _sse(events: list[Json]) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


REPO = {
    "slug": "items", "full_name": "acme-gh/items", "status": "ready", "source": "public_url",
    "languages": {"Python": 9000, "TypeScript": 4000, "CSS": 10}, "file_count": 1234,
    "loc": 56789, "last_indexed_at": "2026-10-30T14:02:00+00:00",
    "last_indexed_sha": "abc1234def", "active_job": None, "index_job_id": None,
}  # fmt: skip


@dataclass
class FakeServer:
    """The API's answers, by path; ``extra`` handlers are asked first."""

    seen: list[httpx.Request] = field(default_factory=list[httpx.Request])
    extra: list[Handler] = field(default_factory=list[Handler])
    polls: list[str] = field(default_factory=lambda: ["authorization_pending", "slow_down"])
    issued: int = 0
    refreshes: int = 0
    revoked: list[str] = field(default_factory=list[str])
    valid: set[str] = field(default_factory=set[str])
    scopes: str = "read:repo ask"
    ask_events: list[Json] = field(default_factory=list[Json])
    repo_states: list[Json] = field(default_factory=list[Json])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        for handler in self.extra:
            if (found := handler(request)) is not None:
                return found
        path = request.url.path
        form = dict(httpx.QueryParams(request.content.decode())) if request.method == "POST" else {}
        match request.method, path:
            case "GET", "/.well-known/oauth-authorization-server":
                base = f"{SERVER}/api/v1/oauth"
                return httpx.Response(200, json={
                    "issuer": SERVER, "token_endpoint": f"{base}/token",
                    "device_authorization_endpoint": f"{base}/device/code",
                    "revocation_endpoint": f"{base}/revoke",
                })  # fmt: skip
            case "GET", "/.well-known/oauth-protected-resource/api":
                return httpx.Response(200, json={"resource": f"{SERVER}/api"})
            case "POST", "/api/v1/oauth/device/code":
                assert form["client_id"] == "sensei-cli"
                assert form["resource"] == f"{SERVER}/api"
                self.scopes = form["scope"]
                return httpx.Response(200, json={
                    "device_code": "rsdc_x", "user_code": "BCDF-GHJK",
                    "verification_uri": f"{SERVER}/device",
                    "verification_uri_complete": f"{SERVER}/device?code=BCDF-GHJK",
                    "expires_in": 600, "interval": 5,
                })  # fmt: skip
            case "POST", "/api/v1/oauth/token":
                return self._token(form)
            case "POST", "/api/v1/oauth/revoke":
                self.revoked.append(form["token"])
                return httpx.Response(200)
            case _:
                pass
        if not self._authorized(request):
            return httpx.Response(
                401, json={"error": {"code": "unauthorized", "message": "Sign in."}}
            )
        return self._api(request)

    def _token(self, form: dict[str, str]) -> httpx.Response:
        if form["grant_type"].endswith("device_code"):
            if self.polls:
                error = self.polls.pop(0)
                return httpx.Response(400, json={"error": error, "error_description": error})
        elif form["grant_type"] == "refresh_token":
            self.refreshes += 1
            if form["refresh_token"] != f"rsrt_{self.issued}":
                return httpx.Response(400, json={"error": "invalid_grant"})
        self.issued += 1
        access = f"rsat_{self.issued}"
        self.valid = {access}
        return httpx.Response(200, json={
            "access_token": access, "refresh_token": f"rsrt_{self.issued}",
            "token_type": "Bearer", "expires_in": 3600, "scope": self.scopes,
        })  # fmt: skip

    def _authorized(self, request: httpx.Request) -> bool:
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        return given in self.valid or given.startswith("rs_")

    def _api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = cast("Json", json.loads(request.content)) if request.content else {}
        match request.method, path:
            case "GET", "/me":
                return httpx.Response(200, json={
                    "login": "ada", "name": "Ada L.",
                    "orgs": [{"slug": "acme", "name": "Acme", "role": "admin"}],
                })  # fmt: skip
            case "GET", "/orgs/acme/repos":
                return httpx.Response(200, json=[
                    REPO,
                    {**REPO, "slug": "web", "full_name": "acme-gh/web", "status": "indexing",
                     "file_count": None, "last_indexed_at": None, "languages": {},
                     "active_job": {"status": "running", "stage": "embed", "progress": 0.42,
                                    "eta_s": 180}},
                ])  # fmt: skip
            case "GET", "/orgs/acme/repos/items":
                state = self.repo_states.pop(0) if self.repo_states else REPO
                return httpx.Response(200, json=state)
            case "POST", "/orgs/acme/repos/items/search":
                return httpx.Response(200, json=_search(body))
            case "POST", "/orgs/acme/repos/items/ask":
                return httpx.Response(
                    200, text=_sse(self.ask_events), headers={"content-type": "text/event-stream"}
                )
            case "POST", "/orgs/acme/repos/items/trace":
                return httpx.Response(200, json=_trace())
            case _:
                pass
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Not found."}})


def _hit(name: str, path: str, layer: str, score: float, lines: list[str]) -> Json:
    return {
        "name": name, "kind": "function", "path": path, "layer": layer, "start_line": 10,
        "end_line": 42, "score": score, "why": ["name"],
        "snippet": {"start_line": 10, "lines": lines, "highlights": []},
    }  # fmt: skip


def _search(body: Json) -> Json:
    lanes = {
        "api": [_hit("login", "backend/app/auth.py", "api", 0.9,
                     ["def login(form):", "    user = check(form)", "    return token(user)",
                      "    # never shown"])],
        "ui": [_hit("LoginForm", "web/src/Login.tsx", "ui", 0.7, ["export function LoginForm() {"]),
               _hit("evil", "web/src/\x1b[2Jevil.ts", "ui", 0.95, ["\x1b]0;pwned\x07const a = 1"])],
        "svc": [], "db": [], "other": [],
    }  # fmt: skip
    return {"query": body["q"], "lanes": lanes, "partial": {"semantic_pending": True}}


def _trace() -> Json:
    def node(kind: str, name: str, path: str, lines: list[int]) -> Json:
        return {"kind": kind, "qualified_name": name, "path": path, "lines": lines}

    hops = [
        {"layer": "ui", "node": node("page", "LoginPage", "web/src/Login.tsx", [5, 60]),
         "edge": None},
        {"layer": "route", "node": node("route", "POST /api/v1/login", "backend/app/auth.py",
                                      [10, 42]),
         "edge": {"type": "calls_api", "confidence": 0.95}},
        {"layer": "db", "node": node("table", "users", "backend/app/models.py", [3, 20]),
         "edge": {"type": "reads", "confidence": 0.9}},
    ]  # fmt: skip
    return {
        "entries": [hops[0]["node"]], "goal": None, "linked": True,
        "paths": [{"hops": hops, "path_confidence": 0.855, "reaches_goal": True,
                   "breaks": [{"after_hop": 2, "reason": "Nothing reads it back.",
                               "kind": "end"}],
                   "entry_index": 0, "return_index": None}],
    }  # fmt: skip


class Cli:
    """The command line, run in-process, and its output's snapshots (kept in ``snapshots``)."""

    def __init__(self, snapshots: Path) -> None:
        self.snapshots = snapshots

    def run(self, *args: str, input: str | None = None) -> Result:
        return CliRunner().invoke(main.app, list(args), input=input)

    def snapshot(self, name: str, text: str) -> None:
        # Lines compared without their trailing spaces (tables pad them; the repository's
        # whitespace hook would take them off the snapshot).
        text = "".join(line.rstrip() + "\n" for line in text.splitlines())
        path = self.snapshots / f"{name}.txt"
        if os.environ.get("SNAPSHOT_UPDATE"):
            path.parent.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assert path.exists(), f"No snapshot {path.name}: run with SNAPSHOT_UPDATE=1 and read it."
        assert text == path.read_text(encoding="utf-8")
