"""Shared conversations (4.11): the link's token, and a conversation as a viewer who can't read
its repository sees it.

A share link carries a random token; only its hash is kept (on the conversation), so a link is
shown once, and asking again replaces it. A viewer who can't read the repository gets the
answers' prose and how sure they are. The code goes: each citation keeps its number and where
it sits in the text, not its file, lines or commit; a code block in an answer (whatever
markdown renders as one: fenced, indented, raw HTML) becomes a note that it is hidden; tool
calls, the research's own words, the searches, the progress notes and an abstention's trail
and suggestion (which name files and symbols) go. Names the prose itself mentions (a function
in backticks, say) stay: they are the answer's words.
"""

from __future__ import annotations

import hashlib
import secrets
from bisect import bisect_right
from typing import Any, cast

from markdown_it import MarkdownIt

TOKEN_BYTES = 32
HIDDEN = "```\n(code hidden: you can't read this repository)\n```"
# What renders as a block of code, read as CommonMark reads it (Streamdown's parser follows it):
# a fenced block, an indented one (after a heading, a fence, a rule or a blank line of spaces
# too), and raw HTML (a <pre>). The page's renderer is what decides, so its parser decides here.
_MD = MarkdownIt("commonmark")
_CODE = frozenset({"fence", "code_block", "html_block"})
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
    texts = [n for n, part in enumerate(parts) if part.get("type") == "text"]
    # The answer is the last text (an answer searched again, or rewritten, comes again whole
    # after the one it replaces): its citations' spans are offsets into it, wherever they sit.
    moved = {n: _Moved() for n in texts}
    last = moved[texts[-1]] if texts else _Moved()
    hidden = {n: moved[n].hide(str(parts[n].get("text", ""))) for n in texts}
    out: list[dict[str, Any]] = []
    for n, part in enumerate(parts):
        kind = part.get("type")
        if kind == "text":
            out.append({**part, "text": hidden[n]})
        elif kind == "data-confidence":
            out.append(part)
        elif kind == "data-citation":
            data = _data(part)
            kept = {"n": data.get("n"), "span": last.span(data.get("span")), "redacted": True}
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


def _code(text: str) -> list[tuple[int, int]]:
    """Where ``text``'s code blocks are (start, end offsets, whole lines), as CommonMark reads
    them, merged where they touch."""
    starts = [0]
    for at, char in enumerate(text):
        if char == "\n":
            starts.append(at + 1)
    found: list[tuple[int, int]] = []
    for token in _MD.parse(text):
        if token.type not in _CODE or token.map is None:
            continue
        first, last = token.map
        start, end = starts[first], starts[last] if last < len(starts) else len(text)
        if end > start and text[end - 1] == "\n":
            end -= 1  # its last line's break stays: the text after it starts a line still
        if found and start <= found[-1][1]:
            found[-1] = (found[-1][0], max(end, found[-1][1]))
        else:
            found.append((start, end))
    return found


class _Moved:
    """A text with its code blocks hidden, and where an offset into it as it was (a citation's
    span, in code points) is now."""

    def __init__(self) -> None:
        self._starts: list[int] = []  # each hidden block's start, as it was
        self._ends: list[int] = []  # and its end
        self._shifts: list[int] = []  # how far everything after it moved

    def hide(self, text: str) -> str:
        out: list[str] = []
        at = shift = 0
        for start, end in _code(text):
            out += [text[at:start], HIDDEN]
            shift += len(HIDDEN) - (end - start)
            self._starts.append(start)
            self._ends.append(end)
            self._shifts.append(shift)
            at = end
        out.append(text[at:])
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
