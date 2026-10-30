"""Shared conversations (4.11): the link's token, and a conversation as a viewer who can't read
its repository sees it.

A share link carries a random token; only its hash is kept (on the conversation), so a link is
shown once, and asking again replaces it. A viewer who can't read the repository gets the
answers' words and how sure they are, and nothing of the code: each citation keeps its number
and where it sits in the text, not its file, lines or commit; tool calls, the research's own
words, the searches and the progress notes go.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any, cast

TOKEN_BYTES = 32
# Kept as they are for any viewer: the answer's words and how sure it is.
_KEPT = frozenset({"text", "data-confidence"})
# Never shown on a shared page: the run behind the answer.
_INTERNAL = frozenset({"data-conversation"})


def new_share_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def share_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def shown(parts: list[dict[str, Any]], *, redact: bool) -> list[dict[str, Any]]:
    """A message's parts as a shared page shows them: whole for a viewer who can read the
    repository (but for the run behind it), else with nothing of the code."""
    if not redact:
        return [part for part in parts if part.get("type") not in _INTERNAL]
    out: list[dict[str, Any]] = []
    for part in parts:
        kind = part.get("type")
        if kind in _KEPT:
            out.append(part)
        elif kind == "data-citation":
            data = _data(part)
            kept = {"n": data.get("n"), "span": data.get("span"), "redacted": True}
            out.append({"type": kind, "id": part.get("id"), "data": kept})
        elif kind == "data-abstain":
            data = _data(part)
            kept: dict[str, Any] = {
                "reason": data.get("reason", ""),
                "searched": [],
                "trailEnd": None,
                "suggestion": data.get("suggestion", ""),
            }
            out.append({"type": kind, "id": part.get("id"), "data": kept})
    return out


def _data(part: dict[str, Any]) -> dict[str, Any]:
    data = part.get("data")
    return cast("dict[str, Any]", data) if isinstance(data, dict) else {}
