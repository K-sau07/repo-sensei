"""Reading a GitHub repository URL as typed by a person."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from sensei_core.errors import InvalidError

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SSH = re.compile(r"^git@(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")


def _invalid() -> InvalidError:
    return InvalidError(
        "Paste a GitHub repository URL, like https://github.com/owner/name.",
        code="invalid_repo_url",
    )


def parse_github_url(value: str, *, web_url: str = "https://github.com") -> tuple[str, str]:
    """``(owner, name)`` from ``https://github.com/owner/name`` and its usual variants
    (``github.com/owner/name``, ``.git``, a trailing ``/tree/main``, ``git@github.com:o/n.git``).
    Anything not on GitHub is refused."""
    text = value.strip()
    allowed = {urlsplit(web_url).hostname or "github.com"}
    allowed |= {f"www.{host}" for host in allowed}

    ssh = _SSH.match(text)
    if ssh:
        host, path = ssh.group("host").lower(), ssh.group("path")
    else:
        if "://" not in text:
            text = f"https://{text}"
        try:
            parts = urlsplit(text)
            host, path = (parts.hostname or "").lower(), parts.path
            credentials = parts.username or parts.password
            port = parts.port
        except ValueError:  # brackets, bad ports, characters that normalise into separators
            raise _invalid() from None
        if parts.scheme not in ("http", "https") or credentials or port is not None:
            raise _invalid()
    if host not in allowed:
        raise _invalid()

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        raise _invalid()
    owner, name = segments[0], segments[1].removesuffix(".git")
    if not _OWNER.match(owner) or not _NAME.match(name) or name in (".", ".."):
        raise _invalid()
    return owner, name
