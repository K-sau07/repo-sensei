"""``sensei``: repo-sensei on the command line.

Signed in with ``sensei login`` (the device flow: approve a code on the web, choose the org),
or with an API key in ``SENSEI_API_KEY`` for scripts. A sign-in is for one org; repositories are
named by their slug in it (``sensei repos list``).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import sys
import time
import webbrowser
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from sensei_cli import config as config_module
from sensei_cli import editors, render
from sensei_cli import login as login_module
from sensei_cli.answers import Answer
from sensei_cli.api import Api, BearerAuth, Session, TokenAuth
from sensei_cli.credentials import Credentials, Keyring
from sensei_cli.errors import CliError

KEY_PREFIX = "rs_"
WATCH_EVERY = 3.0
_GITHUB = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?$"
)


@dataclass(slots=True)
class Runtime:
    """What the commands reach outside: replaced in tests."""

    transport: httpx.BaseTransport | None = None
    keyring: Keyring | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    open_browser: Callable[[str], object] = webbrowser.open


RUNTIME = Runtime()

app = typer.Typer(
    name="sensei",
    help="repo-sensei on the command line: search, ask and trace your repositories.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
repos_app = typer.Typer(help="Your organization's repositories.", no_args_is_help=True)
index_app = typer.Typer(help="Indexing.", no_args_is_help=True)
mcp_app = typer.Typer(help="Connect an editor's agent to repo-sensei.", no_args_is_help=True)
app.add_typer(repos_app, name="repos")
app.add_typer(index_app, name="index")
app.add_typer(mcp_app, name="mcp")


@app.callback()
def _quiet() -> None:
    """Each request isn't news: the HTTP client's own log stays out of the output."""
    logging.getLogger("httpx").setLevel(logging.WARNING)


Repo = Annotated[
    str, typer.Option("--repo", "-r", help="The repository's slug (sensei repos list).")
]
AsJson = Annotated[bool, typer.Option("--json", help="Print the API's answer as JSON.")]


def _out() -> Console:
    return Console(highlight=False)


def _err() -> Console:
    return Console(stderr=True, highlight=False)


def handled[**P](command: Callable[P, None]) -> Callable[P, None]:
    """A command whose :class:`CliError` is said (with what to do) and exits 1."""

    @functools.wraps(command)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            command(*args, **kwargs)
        except CliError as error:
            err = _err()
            err.print(Text(f"Error: {render.clean(error.message)}", "red"))
            if error.hint:
                err.print(Text(render.clean(error.hint)))
            raise typer.Exit(1) from None
        except httpx.TransportError:
            err = _err()
            err.print(Text("Error: The server couldn't be reached.", "red"))
            err.print("Check your connection, or the address with `sensei login --server`.")
            raise typer.Exit(1) from None
        except KeyboardInterrupt:
            raise typer.Exit(130) from None

    return run


# ---------------------------------------------------------------- signing in


def _config() -> config_module.Config:
    found = config_module.load()
    if server := os.environ.get("SENSEI_URL"):
        found.server = config_module.normal_server(server)
    return found


def _credentials(server: str) -> Credentials:
    return Credentials(server, RUNTIME.keyring)


def _api_key() -> str | None:
    key = os.environ.get("SENSEI_API_KEY", "").strip()
    if key and not key.startswith(KEY_PREFIX):
        raise CliError("SENSEI_API_KEY isn't a repo-sensei API key (they start with rs_).")
    return key or None


@contextmanager
def _api() -> Generator[Api]:
    found = _config()
    key = _api_key()
    if key:
        auth: httpx.Auth = BearerAuth(key)
    else:
        session = Session(
            _credentials(found.server), config_module.config_dir(),
            transport=RUNTIME.transport, clock=RUNTIME.clock,
        )  # fmt: skip
        session.tokens  # noqa: B018 - signed in at all? (said before any request)
        auth = TokenAuth(session)
    with Api(found.server, auth, transport=RUNTIME.transport) as api:
        yield api


@app.command()
@handled
def login(
    server: Annotated[
        str | None, typer.Option(help="The repo-sensei server (its web address).")
    ] = None,
    connect: Annotated[
        bool, typer.Option("--connect", help="Also allow connecting repositories (org admins).")
    ] = False,
    browser: Annotated[bool, typer.Option(help="Open the approval page in a browser.")] = True,
) -> None:
    """Sign in: approve a code on the web and choose the organization."""
    found = _config()
    target = config_module.normal_server(server) if server else found.server
    out = _out()
    scopes = [*login_module.SCOPES, *([login_module.CONNECT] if connect else [])]
    with httpx.Client(transport=RUNTIME.transport, timeout=30.0) as http:
        where = login_module.discover(http, target)
        code = login_module.start(http, where, scopes)
        out.print(Text(f"To sign in, open {code.verification_uri} and enter the code:"))
        out.print()
        out.print(Text(f"    {code.user_code}", "bold"))
        out.print()
        if browser and out.is_terminal and code.verification_uri_complete:
            RUNTIME.open_browser(code.verification_uri_complete)
        out.print("Waiting for approval…")
        tokens = login_module.wait(http, where, code, sleep=RUNTIME.sleep, clock=RUNTIME.clock)
    keep = _credentials(target)
    before = keep.load()
    keep.save(tokens)
    if before is not None:  # signed in again: the old sign-in ends on the server too
        login_module.revoke(before, transport=RUNTIME.transport)
    # Just issued: sent as it is.
    with Api(target, BearerAuth(tokens.access_token), transport=RUNTIME.transport) as api:
        me = api.me()
    orgs = render.items(me.get("orgs"))
    org = str(orgs[0]["slug"]) if orgs else None
    config_module.save(config_module.Config(server=target, org=org))
    out.print(Text(f"Signed in as {render.clean(me.get('login'))} to {render.clean(org)}."))
    if _api_key():
        out.print(Text("SENSEI_API_KEY is set: commands use it, not this sign-in.", "yellow"))


@app.command()
@handled
def logout() -> None:
    """Sign out: the sign-in is revoked on the server and forgotten here."""
    found = _config()
    keep = _credentials(found.server)
    tokens = keep.load()
    out = _out()
    if tokens is None:
        out.print("You weren't signed in.")
        return
    told = login_module.revoke(tokens, transport=RUNTIME.transport)
    keep.delete()
    config_module.save(config_module.Config(server=found.server))
    out.print("Signed out." if told else "Signed out here (the server couldn't be reached).")


@app.command()
@handled
def whoami() -> None:
    """Who you're signed in as, where, and with what."""
    with _api() as api:
        me = api.me()
    key = _api_key()
    if key:
        how = f"Using the API key in SENSEI_API_KEY ({key[:11]}…)."
    else:
        tokens = _credentials(_config().server).load()
        scopes = ", ".join(tokens.scopes) if tokens else ""
        how = f"Scopes: {scopes} (the sign-in renews itself)."
    render.whoami(_out(), me, server=api.server, how=how)


# ---------------------------------------------------------------- repositories


@repos_app.command("list")
@handled
def repos_list(as_json: AsJson = False) -> None:
    """The organization's repositories you can read."""
    with _api() as api:
        found = api.get_list(f"/orgs/{api.org()}/repos")
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    render.repos(_out(), found)


def _github_name(url: str) -> str | None:
    """``owner/name`` from a GitHub URL (or ``owner/name`` itself)."""
    text = url.strip()
    if match := _GITHUB.match(text):
        return f"{match.group(1)}/{match.group(2)}"
    if re.fullmatch(r"[\w.-]+/[\w.-]+", text):
        return text
    return None


def _app_repo(api: Api, org: str, full_name: str) -> int | None:
    """A private repository's GitHub id, from what the org's GitHub App installations cover."""
    name = full_name.split("/", 1)[1]
    page = api.get(f"/orgs/{org}/github/repos", {"q": name, "per_page": 100})
    for item in render.items(page.get("items")):
        if str(item.get("full_name", "")).lower() == full_name.lower():
            return int(item["github_repo_id"])
    return None


@repos_app.command("connect")
@handled
def repos_connect(
    url: Annotated[str, typer.Argument(help="A GitHub URL, or owner/name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask before indexing.")] = False,
) -> None:
    """Connect a repository and start indexing it (org admins)."""
    full_name = _github_name(url)
    if full_name is None:
        raise CliError(f"{url!r} isn't a GitHub repository.", hint="Give its URL or owner/name.")
    out = _out()
    with _api() as api:
        org = api.org()
        body: dict[str, Any] = {"url": f"https://github.com/{full_name}"}
        try:
            found = api.post(f"/orgs/{org}/repos/estimate", body)
        except CliError as error:
            if error.code != "repo_not_public":
                raise
            repo_id = _app_repo(api, org, full_name)
            if repo_id is None:
                raise CliError(
                    f"{full_name} isn't public, and the organization's GitHub App doesn't "
                    "cover it.",
                    hint=f"Install the app on it: {api.server}/{org}/repos/new",
                ) from None
            body = {"github_repo_id": repo_id}
            found = api.post(f"/orgs/{org}/repos/estimate", body)
        render.estimate(out, found)
        if not yes and not typer.confirm("Connect it and start indexing?", default=True):
            out.print("Not connected.")
            raise typer.Exit(1)
        path = "/repos" if "github_repo_id" in body else "/repos/import"
        repo = api.post(f"/orgs/{org}{path}", body)
    slug = render.clean(repo.get("slug"))
    out.print(Text(f"Connected as {slug}."))
    if repo.get("index_job_id"):
        out.print(Text(f"Indexing has started: `sensei index status {slug} --watch` follows it."))


@index_app.command("status")
@handled
def index_status(
    repo: Annotated[str, typer.Argument(help="The repository's slug.")],
    watch: Annotated[bool, typer.Option("--watch", "-w", help="Follow it until done.")] = False,
) -> None:
    """A repository's indexing: what is running, how far along, and the last index."""
    out = _out()
    with _api() as api:
        path = api.repo_path(repo)
        found = api.get(path)
        if watch and found.get("active_job"):
            out.print(Text(f"Following {render.clean(found.get('full_name'))}…"))
            seen = None
            while found.get("active_job"):
                job = found["active_job"]
                # A line per stage, and per 5% within one.
                now = (job.get("stage"), round(float(job.get("progress") or 0) * 20))
                if now != seen:
                    render.progress(out, found)
                    seen = now
                RUNTIME.sleep(WATCH_EVERY)
                found = api.get(path)
    render.index_status(out, found)


# ---------------------------------------------------------------- search, ask, trace


@app.command()
@handled
def search(
    query: Annotated[str, typer.Argument(help="What to look for, in words or by name.")],
    repo: Repo,
    limit: Annotated[int, typer.Option(min=1, max=50, help="How many hits.")] = 10,
    layer: Annotated[
        list[str] | None, typer.Option(help="Only these layers: ui, api, svc, db, other.")
    ] = None,
    as_json: AsJson = False,
) -> None:
    """Search a repository's code by meaning and by name, best first."""
    body: dict[str, Any] = {"q": query, "limit": min(limit, 50)}
    if layer:
        body["layers"] = layer
    with _api() as api:
        found = api.post(api.repo_path(repo, "/search"), body)
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    render.search(_out(), found, limit)


@app.command()
@handled
def ask(
    question: Annotated[str, typer.Argument(help="Your question about the code.")],
    repo: Repo,
    deep: Annotated[
        bool, typer.Option("--deep", help="Research longer (spends more of the budget).")
    ] = False,
    as_json: AsJson = False,
) -> None:
    """Ask about a repository: an answer with citations (spends the org's AI budget)."""
    out = _out()
    answer = Answer()
    body = {"message": question, "mode": "deep" if deep else "fast"}
    with _api() as api:
        events = api.stream(api.repo_path(repo, "/ask"), body)
        if out.is_terminal and not as_json:
            with Live(console=out, refresh_per_second=8, transient=True) as live:
                for event in events:
                    answer.take(event)
                    live.update(live_view(answer))
        else:
            for event in events:
                answer.take(event)
    if answer.error:
        raise CliError(answer.error)
    if as_json:
        typer.echo(json.dumps({
            "answer": answer.text, "citations": answer.cited,
            "confidence": answer.confidence, "abstained": answer.abstained,
        }, indent=2))  # fmt: skip
        return
    if answer.abstained:
        render.abstained(out, answer.abstained)
    if answer.text:
        text = render.clean(answer.text)
        out.print(Markdown(text) if out.is_terminal else Text(text))
    render.citations(out, answer.cited)
    if answer.confidence:
        out.print(Text(f"Confidence: {render.clean(answer.confidence)}", "dim"))


def live_view(answer: Answer) -> Group:
    """What an answer shows while it streams: the text so far, and what is being done."""
    spinner = Spinner("dots", Text(answer.doing, "dim"))
    if not answer.text:
        return Group(spinner)
    return Group(Markdown(render.clean(answer.text)), Text(), spinner)


@app.command()
@handled
def trace(
    start: Annotated[
        str, typer.Argument(help="A symbol, a route ('POST /api/login'), a file, or a feature.")
    ],
    repo: Repo,
    to: Annotated[str | None, typer.Option(help="Where it should end, if known.")] = None,
    direction: Annotated[
        str, typer.Option(help="both, down (to the data) or up (back to the pages).")
    ] = "both",
    as_json: AsJson = False,
) -> None:
    """Follow a feature across the layers: page, API, services, data."""
    if direction not in ("both", "down", "up"):
        raise CliError("--direction is both, down or up.")
    body: dict[str, Any] = {"from": start, "direction": direction}
    if to:
        body["to"] = to
    with _api() as api:
        found = api.post(api.repo_path(repo, "/trace"), body)
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    render.trace(_out(), found, start)


# ---------------------------------------------------------------- MCP


@mcp_app.command("config")
@handled
def mcp_config(
    client: Annotated[editors.Client, typer.Option(help="The editor.")],
    local: Annotated[
        bool, typer.Option("--local", help="Run the server locally (stdio, with an API key).")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print only the config.")] = False,
) -> None:
    """The config that connects an editor's agent to repo-sensei."""
    found = _config()
    setup = editors.setup(client, found.server, local=local)
    snippet = json.dumps(setup.config, indent=2)
    if as_json:
        typer.echo(snippet)
        return
    out = _out()
    if setup.command:
        out.print(Text("Run:", "bold"))
        out.print(Text(f"  {setup.command}"), soft_wrap=True)
        out.print()
        out.print(Text(f"or put this in {setup.file}:", "bold"))
    else:
        out.print(Text(f"Put this in {setup.file}:", "bold"))
    out.print(Text(snippet), soft_wrap=True)
    out.print()
    out.print(Text(setup.then))
    if local:
        org = found.org or "<org>"
        out.print(Text(f"Create an API key at {found.server}/{org}/settings/api-keys."))


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
