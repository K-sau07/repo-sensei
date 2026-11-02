"""``sensei-mcp``: the MCP server over stdio, for an editor to start on this machine.

It talks to a repo-sensei API with an API key (an org's, made in the web app's settings):

    SENSEI_URL=https://… SENSEI_API_KEY=rs_… sensei-mcp

Following the MCP authorization spec, a stdio server takes its credentials from the
environment, not OAuth.
"""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.mcpserver import Context

from sensei_mcp.api import SenseiApi
from sensei_mcp.server import build_server


def main() -> None:
    url = os.environ.get("SENSEI_URL", "").strip()
    key = os.environ.get("SENSEI_API_KEY", "").strip()
    if not url or not key.startswith("rs_"):
        sys.stderr.write(
            "sensei-mcp needs SENSEI_URL (your repo-sensei address) and SENSEI_API_KEY (an API "
            "key, rs_…, from Settings → API keys).\n"
        )
        raise SystemExit(2)
    # A line per API call on the editor's log is noise (and names the URLs asked).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    api = SenseiApi(url, f"Bearer {key}")

    async def api_for(_ctx: Context) -> SenseiApi:
        return api

    build_server(api_for).run("stdio")


if __name__ == "__main__":
    main()
