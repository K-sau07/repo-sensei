"""``sensei``'s commands against a stand-in server: signing in (the device flow; tokens in the
keyring, never in the config file), refreshing (once, and not twice over), signing out, and each
command's output (snapshots)."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from keyring.errors import NoKeyringError
from rich.console import Console

from sensei_cli import credentials, main
from sensei_cli.answers import Answer
from sensei_cli.api import Session
from sensei_cli.credentials import Credentials, Tokens
from sensei_testing.cli import SERVER, Cli, FakeServer, MemoryKeyring

KEPT = (credentials.SERVICE, SERVER)
Json = dict[str, Any]


def _signed_in(keys: MemoryKeyring, server: FakeServer, clock: list[float], **over: object) -> None:
    server.issued = 1
    server.valid = {"rsat_1"}
    tokens = Tokens(
        access_token="rsat_1", refresh_token="rsrt_1", expires_at=clock[0] + 3600,
        scopes=["read:repo", "ask"], token_endpoint=f"{SERVER}/api/v1/oauth/token",
        revocation_endpoint=f"{SERVER}/api/v1/oauth/revoke", resource=f"{SERVER}/api",
    )  # fmt: skip
    for name, value in over.items():
        setattr(tokens, name, value)
    keys.set_password(*KEPT, tokens.dumps())


def test_login_waits_for_approval_and_keeps_the_tokens_in_the_keyring(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, tmp_path: Path
) -> None:
    result = cli.run("login")
    assert result.exit_code == 0, result.output
    cli.snapshot("login", result.output)
    # Polled after pending and slow_down, the interval widened by 5 seconds.
    assert server.scopes == "read:repo ask"
    kept = Tokens.loads(keys.kept[KEPT])
    assert kept is not None
    assert (kept.access_token, kept.refresh_token) == ("rsat_1", "rsrt_1")
    assert kept.resource == f"{SERVER}/api"
    # The config file has the server and org only, and is the user's alone.
    config = tmp_path / "config" / "config.toml"
    assert config.read_text() == f'server = "{SERVER}"\norg = "acme"\n'
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert "rsat_" not in config.read_text()


def test_login_with_connect_asks_for_the_connect_scope(cli: Cli, server: FakeServer) -> None:
    assert cli.run("login", "--connect").exit_code == 0
    assert server.scopes == "read:repo ask connect:repo"


def test_a_declined_or_expired_login_says_so(
    cli: Cli, server: FakeServer, keys: MemoryKeyring
) -> None:
    server.polls = ["access_denied"]
    declined = cli.run("login")
    assert declined.exit_code == 1
    assert "Signing in was declined." in declined.output
    server.polls = ["authorization_pending"] * 200
    expired = cli.run("login")
    assert expired.exit_code == 1
    assert "The code expired before it was approved." in expired.output
    assert not keys.kept


def test_signing_in_again_ends_the_old_sign_in(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    assert cli.run("login").exit_code == 0
    assert server.revoked == ["rsrt_1"]
    kept = Tokens.loads(keys.kept[KEPT])
    assert kept is not None
    assert kept.access_token == "rsat_2"


def test_a_server_whose_metadata_names_another_issuer_is_refused(
    cli: Cli, server: FakeServer, keys: MemoryKeyring
) -> None:
    def elsewhere(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"issuer": "https://evil.example"})
        return None

    server.extra.append(elsewhere)
    result = cli.run("login")
    assert result.exit_code == 1
    assert "signs in as 'https://evil.example', not itself" in result.output
    assert not any(r.url.path.endswith("/device/code") for r in server.seen)
    assert not keys.kept


def test_a_server_that_cant_be_reached_while_signing_in(cli: Cli, server: FakeServer) -> None:
    def down(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/device/code"):
            raise httpx.ConnectError("refused", request=request)
        return None

    server.extra.append(down)
    result = cli.run("login")
    assert result.exit_code == 1
    assert "The server couldn't be reached." in result.output


def test_an_expired_access_token_is_refreshed_and_kept(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock, expires_at=clock[0] + 30)  # inside the early margin
    result = cli.run("whoami")
    assert result.exit_code == 0, result.output
    assert server.refreshes == 1
    kept = Tokens.loads(keys.kept[KEPT])
    assert kept is not None
    assert (kept.access_token, kept.refresh_token) == ("rsat_2", "rsrt_2")
    cli.snapshot("whoami", result.output)


def test_a_refused_token_is_refreshed_once_and_the_request_sent_again(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    server.valid = set()  # revoked on the server: 401
    sent: list[str | None] = []

    def note(request: httpx.Request) -> None:
        if request.url.path.endswith("/me"):
            sent.append(request.headers.get("authorization"))

    server.extra.append(note)
    result = cli.run("repos", "list")
    assert result.exit_code == 0, result.output
    assert server.refreshes == 1
    assert sent == ["Bearer rsat_1", "Bearer rsat_2"]


def test_a_token_another_command_refreshed_is_used_not_refreshed_again(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock, expires_at=clock[0] - 1)
    newer = Tokens.loads(keys.kept[KEPT])
    assert newer is not None

    def meanwhile(request: httpx.Request) -> httpx.Response | None:
        # Between this command reading the keyring and refreshing: another command refreshed.
        if request.url.path == "/api/v1/oauth/token":
            raise AssertionError("refreshed again: the reused refresh token would sign out")
        return None

    server.extra.append(meanwhile)
    newer.access_token, newer.refresh_token, newer.expires_at = "rsat_9", "rsrt_9", clock[0] + 3600
    session = Session(Credentials(SERVER, keys), Path("/tmp"), clock=lambda: clock[0])  # noqa: S108
    assert session.tokens.access_token == "rsat_1"  # read before the other command's refresh
    keys.set_password(*KEPT, newer.dumps())
    assert session.access() == "rsat_9"


def test_a_refresh_token_refused_signs_out_and_says_to_sign_in(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock, expires_at=clock[0] - 1, refresh_token="rsrt_old")
    result = cli.run("repos", "list")
    assert result.exit_code == 1
    assert "Your sign-in has expired." in result.output
    assert "sensei login" in result.output
    assert KEPT not in keys.kept


def test_logout_revokes_the_family_and_forgets_it(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    result = cli.run("logout")
    assert (result.exit_code, result.output) == (0, "Signed out.\n")
    assert server.revoked == ["rsrt_1"]
    assert KEPT not in keys.kept
    assert cli.run("logout").output == "You weren't signed in.\n"
    signed_out = cli.run("repos", "list")
    assert signed_out.exit_code == 1
    assert "You're not signed in." in signed_out.output


def test_an_api_key_from_the_environment_is_used_as_it_is(
    cli: Cli, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENSEI_API_KEY", "rs_AbCdEfGh_" + "x" * 43)
    result = cli.run("whoami")
    assert result.exit_code == 0, result.output
    assert "Using the API key in SENSEI_API_KEY (rs_AbCdEfGh…)." in result.output
    assert "x" * 10 not in result.output  # the secret isn't shown
    assert server.seen[-1].headers["authorization"] == "Bearer rs_AbCdEfGh_" + "x" * 43
    monkeypatch.setenv("SENSEI_API_KEY", "ghp_nope")
    wrong = cli.run("whoami")
    assert wrong.exit_code == 1
    assert "isn't a repo-sensei API key" in wrong.output


def test_repos_list(cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]) -> None:
    _signed_in(keys, server, clock)
    result = cli.run("repos", "list")
    assert result.exit_code == 0, result.output
    cli.snapshot("repos_list", result.output)
    as_json = cli.run("repos", "list", "--json")
    assert [r["slug"] for r in json.loads(as_json.output)] == ["items", "web"]


def test_index_status_and_watching_it(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    job = {"status": "running", "stage": "parse", "progress": 0.1, "eta_s": 300}
    server.repo_states = [
        {**REPO_RUNNING, "active_job": job},
        {**REPO_RUNNING, "active_job": {**job, "progress": 0.11}},  # not a step: not shown
        {**REPO_RUNNING, "active_job": {**job, "stage": "embed", "progress": 0.6, "eta_s": 60}},
    ]
    result = cli.run("index", "status", "items", "--watch")
    assert result.exit_code == 0, result.output
    cli.snapshot("index_status_watch", result.output)


REPO_RUNNING = {
    "slug": "items", "full_name": "acme-gh/items", "status": "indexing",
    "last_indexed_at": None, "last_indexed_sha": None, "file_count": None, "loc": None,
}  # fmt: skip


def test_search_is_best_first_and_repository_text_cant_drive_the_terminal(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    result = cli.run("search", "where is login", "--repo", "items", "--limit", "3")
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "\x07" not in result.output
    cli.snapshot("search", result.output)
    sent = json.loads(server.seen[-1].content)
    assert sent == {"q": "where is login", "limit": 3}


def test_ask_prints_the_answer_its_sources_and_how_sure(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    server.ask_events = [
        {"type": "start", "messageId": "m1"},
        {"type": "reasoning-start", "id": "reasoning-1"},
        {"type": "reasoning-delta", "id": "reasoning-1", "delta": "Let me look."},
        {"type": "tool-input-available", "toolCallId": "c1", "toolName": "code_search",
         "input": {"query": "login"}},
        {"type": "text-start", "id": "text-2"},
        {"type": "text-delta", "id": "text-2", "delta": "Login checks "},
        {"type": "text-delta", "id": "text-2", "delta": "the form [1]."},
        {"type": "data-citation", "id": "citation-1",
         "data": {"n": 1, "path": "backend/app/auth.py", "lines": [10, 42], "verified": True}},
        {"type": "data-citation", "id": "citation-2",
         "data": {"n": 2, "path": "web/src/Login.tsx", "lines": [7, 7], "verified": False}},
        # Verification rewrote it: sent again, whole; this is the answer.
        {"type": "text-start", "id": "text-answer"},
        {"type": "text-delta", "id": "text-answer",
         "delta": "Login checks the password [1] from the form [2]."},
        {"type": "data-confidence", "id": "confidence", "data": {"level": "high"}},
        {"type": "finish"},
    ]  # fmt: skip
    result = cli.run("ask", "how does login work?", "--repo", "items")
    assert result.exit_code == 0, result.output
    cli.snapshot("ask", result.output)
    assert json.loads(server.seen[-1].content) == {
        "message": "how does login work?", "mode": "fast",
    }  # fmt: skip


def test_ask_that_abstains_or_fails(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    server.ask_events = [
        {"type": "data-abstain", "id": "abstain", "data": {
            "reason": "No code handles payments.", "searched": ["payment", "stripe"],
            "suggestion": "Ask about orders instead."}},
        {"type": "finish"},
    ]  # fmt: skip
    abstained = cli.run("ask", "payments?", "--repo", "items", "--deep")
    assert abstained.exit_code == 0, abstained.output
    cli.snapshot("ask_abstained", abstained.output)
    assert json.loads(server.seen[-1].content)["mode"] == "deep"
    server.ask_events = [{"type": "error", "errorText": "The budget is spent."}]
    failed = cli.run("ask", "x?", "--repo", "items")
    assert failed.exit_code == 1
    assert "Error: The budget is spent." in failed.output


def test_trace(cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]) -> None:
    _signed_in(keys, server, clock)
    result = cli.run("trace", "LoginPage", "--repo", "items", "--direction", "down")
    assert result.exit_code == 0, result.output
    cli.snapshot("trace", result.output)
    assert json.loads(server.seen[-1].content) == {"from": "LoginPage", "direction": "down"}


def test_errors_say_what_to_do(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    missing = cli.run("search", "x", "--repo", "gone")
    assert missing.exit_code == 1
    assert "Error: Not found." in missing.output

    def unreachable(request: httpx.Request) -> httpx.Response | None:
        raise httpx.ConnectError("refused", request=request)

    server.extra.append(unreachable)
    down = cli.run("repos", "list")
    assert down.exit_code == 1
    assert f"Couldn't reach {SERVER}." in down.output


ESTIMATE: Json = {
    "full_name": "langflow-ai/langflow", "private": False, "size_kb": 90000, "files": 4321,
    "tokens": 9_876_543, "index_minutes": 12.4, "search_ready_minutes": 4,
    "summaries_minutes": 8, "cost_usd": "1.37", "unpriced": [],
}  # fmt: skip


def _connecting(server: FakeServer, *, public: bool = True) -> list[tuple[str, Json]]:
    """Estimates and connections, answered; what was asked, noted."""
    asked: list[tuple[str, Json]] = []

    def answer(request: httpx.Request) -> httpx.Response | None:
        path = request.url.path.removeprefix("/api/v1/orgs/acme")
        body = cast("Json", json.loads(request.content)) if request.content else {}
        if request.method == "POST" and path.startswith("/repos"):
            asked.append((path, body))
        match path:
            case "/repos/estimate" if "url" in body and not public:
                error = {"code": "repo_not_public", "message": "Not public."}
                return httpx.Response(422, json={"error": error})
            case "/repos/estimate":
                return httpx.Response(200, json={**ESTIMATE, "private": not public})
            case "/repos/import" | "/repos":
                return httpx.Response(
                    201, json={**REPO_RUNNING, "slug": "langflow", "index_job_id": "j1"}
                )
            case "/github/repos":
                assert request.url.params["q"] == "langflow"
                items = [{"full_name": "Langflow-AI/langflow", "github_repo_id": 77}]
                return httpx.Response(200, json={"items": items})
            case _:
                return None

    server.extra.append(answer)
    return asked


def test_connecting_a_public_repository_shows_the_estimate_first(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    asked = _connecting(server)
    declined = cli.run("repos", "connect", "github.com/langflow-ai/langflow", input="n\n")
    assert declined.exit_code == 1
    assert declined.output.endswith("Not connected.\n")
    assert [path for path, _ in asked] == ["/repos/estimate"]  # nothing connected
    result = cli.run(
        "repos", "connect", "https://github.com/langflow-ai/langflow/tree/main", input="y\n"
    )
    assert result.exit_code == 0, result.output
    cli.snapshot("repos_connect", result.output)
    assert asked[-1] == ("/repos/import", {"url": "https://github.com/langflow-ai/langflow"})


def test_a_private_repository_connects_through_the_github_app(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    asked = _connecting(server, public=False)
    result = cli.run("repos", "connect", "langflow-ai/langflow", "--yes")
    assert result.exit_code == 0, result.output
    assert asked == [
        ("/repos/estimate", {"url": "https://github.com/langflow-ai/langflow"}),
        ("/repos/estimate", {"github_repo_id": 77}),
        ("/repos", {"github_repo_id": 77}),
    ]
    assert "langflow-ai/langflow · private" in result.output


def test_connecting_without_the_scope_says_how_to_get_it(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)

    def refuse(request: httpx.Request) -> httpx.Response | None:
        if not request.url.path.endswith("/repos/estimate"):
            return None
        error = {"code": "insufficient_scope", "message": "This token doesn't have the "
                 "connect:repo scope.", "details": {"required_scope": "connect:repo"}}  # fmt: skip
        challenge = 'Bearer error="insufficient_scope", scope="connect:repo"'
        return httpx.Response(403, json={"error": error}, headers={"WWW-Authenticate": challenge})

    server.extra.append(refuse)
    result = cli.run("repos", "connect", "langflow-ai/langflow")
    assert result.exit_code == 1
    assert "Run `sensei login --connect`" in result.output
    assert cli.run("repos", "connect", "not a repo").exit_code == 1


@pytest.mark.parametrize("client", ["claude-code", "vscode", "cursor"])
@pytest.mark.parametrize("local", [False, True])
def test_mcp_config_for_each_editor(cli: Cli, client: str, local: bool) -> None:
    args = ["mcp", "config", "--client", client, *(["--local"] if local else [])]
    result = cli.run(*args)
    assert result.exit_code == 0, result.output
    cli.snapshot(f"mcp_{client}_{'local' if local else 'remote'}", result.output)
    config = json.loads(cli.run(*args, "--json").output)
    root = "servers" if client == "vscode" else "mcpServers"
    entry = config[root]["repo-sensei"]
    if local:
        assert entry["command"] == "sensei-mcp"
        assert entry["env"]["SENSEI_URL"] == SERVER
        assert not entry["env"]["SENSEI_API_KEY"].startswith("rs_")  # never the key itself
    else:
        assert entry["url"] == f"{SERVER}/mcp"


def test_without_a_keyring_it_says_to_use_an_api_key(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, runtime: main.Runtime
) -> None:
    class Locked(MemoryKeyring):
        def get_password(self, service: str, username: str, /) -> str | None:
            raise NoKeyringError("none")

    runtime.keyring = Locked()
    result = cli.run("whoami")
    assert result.exit_code == 1
    assert "No system keyring" in result.output
    assert "SENSEI_API_KEY" in result.output


def test_json_output_for_scripts(
    cli: Cli, server: FakeServer, keys: MemoryKeyring, clock: list[float]
) -> None:
    _signed_in(keys, server, clock)
    found = json.loads(cli.run("search", "login", "--repo", "items", "--json").output)
    assert set(found["lanes"]) == {"ui", "api", "svc", "db", "other"}
    server.ask_events = [
        {"type": "text-start", "id": "text-1"},
        {"type": "text-delta", "id": "text-1", "delta": "It hashes [1]."},
        {"type": "data-citation", "id": "citation-1",
         "data": {"n": 1, "path": "a.py", "lines": [1, 2], "verified": True}},
    ]  # fmt: skip
    asked = json.loads(cli.run("ask", "how?", "--repo", "items", "--json").output)
    assert asked["answer"] == "It hashes [1]."
    assert asked["citations"][0]["path"] == "a.py"
    traced = json.loads(cli.run("trace", "LoginPage", "--repo", "items", "--json").output)
    assert traced["paths"][0]["reaches_goal"] is True


def test_the_live_view_shows_what_is_being_done_then_the_text() -> None:
    answer = Answer()
    answer.take({"type": "tool-input-available", "toolName": "code_search",
                 "input": {"query": "password \x1b[2J hashing"}})  # fmt: skip
    shown = io.StringIO()
    console = Console(file=shown, width=80, color_system=None, force_terminal=False)
    console.print(main.live_view(answer))
    assert "Searching: password \\x1b[2J hashing" in shown.getvalue()
    answer.take({"type": "text-start", "id": "text-1"})
    answer.take({"type": "text-delta", "id": "text-1", "delta": "It **hashes**."})
    console.print(main.live_view(answer))
    assert "It hashes." in shown.getvalue()
