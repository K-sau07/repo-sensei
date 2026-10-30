"""Shared conversations (4.11): the link's token, and a conversation as a viewer who can't read
its repository sees it.

A share link carries a random token; only its hash is kept (on the conversation), so a link is
shown once, and asking again replaces it. A viewer who can't read the repository gets the
answers' prose and how sure they are. The code goes: each citation keeps its number and where
it sits in the text, not its file, lines or commit; a fenced code block in an answer becomes a
note that it is hidden; tool calls, the research's own words, the searches, the progress notes
and an abstention's trail and suggestion (which name files and symbols) go. Names the prose
itself mentions (a function in backticks, say) stay: they are the answer's words.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from bisect import bisect_right
from typing import Any, cast

TOKEN_BYTES = 32
HIDDEN = "```\n(code hidden: you can't read this repository)\n```"
# Code in an answer, as markdown has it: a fenced block (``` or ~~~, three or more, at a line's
# start however indented: in a list item too) to the same fence or the end; or an indented block
# (lines indented four spaces or a tab, after a blank line). Prose indented as much (a list's
# second paragraph) is hidden too: better too much than any code.
_FENCE = re.compile(
    r"^[ \t]*(?P<f>`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*(?P=f)[ \t]*$|\Z)"
    r"|(?:(?<=\n\n)|\A)(?:(?: {4,}|\t)[^\n]*(?:\n|\Z))+",
    re.M | re.S,
)
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
    moved = _Moved()
    out: list[dict[str, Any]] = []
    for part in parts:
        kind = part.get("type")
        if kind == "text":
            out.append({**part, "text": moved.hide(str(part.get("text", "")))})
        elif kind == "data-confidence":
            out.append(part)
        elif kind == "data-citation":
            data = _data(part)
            kept = {"n": data.get("n"), "span": moved.span(data.get("span")), "redacted": True}
            out.append({"type": kind, "id": part.get("id"), "data": kept})
        elif kind == "data-abstain":
            data = _data(part)
            kept: dict[str, Any] = {
                "reason": data.get("reason", ""),
                "searched": [],
                "trailEnd": None,
                "suggestion": "",
            }
            out.append({"type": kind, "id": part.get("id"), "data": kept})
    return out


class _Moved:
    """The answer's text with its code blocks hidden, and where an offset into the text as it
    was (a citation's span: the text parts joined, in code points) is now."""

    def __init__(self) -> None:
        self._read = 0  # the text so far, as it was
        self._starts: list[int] = []  # each hidden block's start, as it was
        self._ends: list[int] = []  # and its end
        self._shifts: list[int] = []  # how far everything after it moved

    def hide(self, text: str) -> str:
        out: list[str] = []
        at = 0
        shift = self._shifts[-1] if self._shifts else 0
        for block in _FENCE.finditer(text):
            out += [text[at : block.start()], HIDDEN]
            shift += len(HIDDEN) - (block.end() - block.start())
            self._starts.append(self._read + block.start())
            self._ends.append(self._read + block.end())
            self._shifts.append(shift)
            at = block.end()
        out.append(text[at:])
        self._read += len(text)
        return "".join(out)

    def span(self, span: Any) -> list[int] | None:
        if not (isinstance(span, list | tuple) and len(cast("list[Any]", span)) == 2):
            return None
        start, end = cast("list[Any]", span)
        if not (isinstance(start, int) and isinstance(end, int)):
            return None
        return [self._at(start), self._at(end)]

    def _at(self, offset: int) -> int:
        n = bisect_right(self._ends, offset)  # the blocks that end at or before it
        if n < len(self._starts) and self._starts[n] < offset:  # inside a block: after its note
            return self._ends[n] + self._shifts[n]
        return offset + (self._shifts[n - 1] if n else 0)


def _data(part: dict[str, Any]) -> dict[str, Any]:
    data = part.get("data")
    return cast("dict[str, Any]", data) if isinstance(data, dict) else {}
