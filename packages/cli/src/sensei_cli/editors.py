"""``sensei mcp config``: how to connect an editor's agent to repo-sensei's MCP server.

Two ways, for each client:

* remote: the server's ``/mcp`` over HTTP; the editor signs in with OAuth by itself the first
  time (the server's 401 says where);
* local: ``sensei-mcp`` run by the editor over stdio, with an API key from the environment (or,
  in VS Code, asked for once and kept in its secret storage). The key is never written into the
  project's config file, which may be committed.

Each client's file and root key: Claude Code ``.mcp.json`` (``mcpServers``), VS Code
``.vscode/mcp.json`` (``servers``), Cursor ``.cursor/mcp.json`` (``mcpServers``).
"""

from __future__ import annotations

import enum
import json
import shlex
from dataclasses import dataclass
from typing import Any

NAME = "repo-sensei"
COMMAND = "sensei-mcp"
KEY_INPUT = "sensei-api-key"


class Client(enum.StrEnum):
    CLAUDE_CODE = "claude-code"
    VSCODE = "vscode"
    CURSOR = "cursor"


@dataclass(frozen=True, slots=True)
class Setup:
    file: str  # where the JSON goes, in the project
    config: dict[str, Any]
    command: str | None  # a one-liner that adds it instead
    then: str  # what to do next


def setup(client: Client, server: str, *, local: bool) -> Setup:
    url = f"{server}/mcp"
    match client, local:
        case Client.CLAUDE_CODE, False:
            return Setup(
                ".mcp.json",
                {"mcpServers": {NAME: {"type": "http", "url": url}}},
                f"claude mcp add --transport http {NAME} {shlex.quote(url)}",
                f"In Claude Code, run /mcp and choose {NAME} to sign in.",
            )
        case Client.CLAUDE_CODE, True:
            env = {"SENSEI_URL": server, "SENSEI_API_KEY": "${SENSEI_API_KEY}"}
            return Setup(
                ".mcp.json",
                {"mcpServers": {NAME: {"type": "stdio", "command": COMMAND, "env": env}}},
                f"claude mcp add --env SENSEI_URL={shlex.quote(server)} "
                f'--env SENSEI_API_KEY="$SENSEI_API_KEY" --transport stdio {NAME} -- {COMMAND}',
                "Set SENSEI_API_KEY in the environment Claude Code starts from.",
            )
        case Client.VSCODE, False:
            entry = {"type": "http", "url": url}
            return Setup(
                ".vscode/mcp.json",
                {"servers": {NAME: entry}},
                f"code --add-mcp {shlex.quote(json.dumps({'name': NAME, **entry}))}",
                "Start the server from the MCP view and sign in when VS Code asks.",
            )
        case Client.VSCODE, True:
            prompt = {
                "type": "promptString", "id": KEY_INPUT,
                "description": "repo-sensei API key", "password": True,
            }  # fmt: skip
            env = {"SENSEI_URL": server, "SENSEI_API_KEY": f"${{input:{KEY_INPUT}}}"}
            return Setup(
                ".vscode/mcp.json",
                {
                    "inputs": [prompt],
                    "servers": {NAME: {"type": "stdio", "command": COMMAND, "env": env}},
                },
                None,
                "VS Code asks for the API key the first time the server starts, and keeps it.",
            )
        case Client.CURSOR, False:
            return Setup(
                ".cursor/mcp.json",
                {"mcpServers": {NAME: {"url": url}}},
                None,
                f"In Cursor's MCP settings, sign in to {NAME} when it asks.",
            )
        case Client.CURSOR, True:
            env = {"SENSEI_URL": server, "SENSEI_API_KEY": "${env:SENSEI_API_KEY}"}
            return Setup(
                ".cursor/mcp.json",
                {"mcpServers": {NAME: {"type": "stdio", "command": COMMAND, "env": env}}},
                None,
                "Set SENSEI_API_KEY in the environment Cursor starts from.",
            )
    raise AssertionError(client)  # pragma: no cover - every client is matched
