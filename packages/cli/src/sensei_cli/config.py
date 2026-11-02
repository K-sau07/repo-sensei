"""Where the CLI keeps its settings: ``~/.config/repo-sensei/config.toml`` (``XDG_CONFIG_HOME``
honoured; ``SENSEI_CONFIG_DIR`` overrides it).

Only the server and the org signed in to are kept here. Tokens are in the OS keyring
(:mod:`sensei_cli.credentials`), never in this file.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SERVER = "http://localhost:3000"
FILE = "config.toml"


@dataclass(slots=True)
class Config:
    server: str = DEFAULT_SERVER
    org: str | None = None  # the org the sign-in is for (a token is for one)


def config_dir() -> Path:
    if found := os.environ.get("SENSEI_CONFIG_DIR"):
        return Path(found)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "repo-sensei"


def normal_server(url: str) -> str:
    """A server's origin as kept (no trailing slash): what tokens are filed under."""
    return url.strip().rstrip("/")


LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def check_server(url: str) -> str:
    """``url`` as a server address, if it's one to send tokens to: HTTPS, or HTTP on this
    computer only (anything else would carry them in the clear). ValueError otherwise."""
    parts = urlsplit(normal_server(url))
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return normal_server(url)
    if parts.scheme == "http" and host in LOOPBACK:
        return normal_server(url)
    raise ValueError(f"{url} isn't an https:// address (http:// is only for this computer)")


def load() -> Config:
    path = config_dir() / FILE
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return Config()  # unreadable: as if there were none (saving writes it anew)
    server = raw.get("server")
    org = raw.get("org")
    return Config(
        server=normal_server(server) if isinstance(server, str) and server else DEFAULT_SERVER,
        org=org if isinstance(org, str) and org else None,
    )


def save(config: Config) -> None:
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # JSON strings are TOML basic strings (the same escapes).
    lines = [f"server = {json.dumps(config.server)}"]
    if config.org:
        lines.append(f"org = {json.dumps(config.org)}")
    path = folder / FILE
    partial = path.with_suffix(".toml.partial")
    partial.write_text("\n".join(lines) + "\n", encoding="utf-8")
    partial.chmod(0o600)
    partial.replace(path)
