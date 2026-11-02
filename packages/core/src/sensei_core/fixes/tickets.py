"""Turning what a user pastes (a stack trace, an issue's text) into a structured ticket (8.3).

Parsed, never run: Python tracebacks (chained ones too: the last exception is the one raised),
Node and Chrome-style stacks (``at fn (file:line:col)``), Firefox and Safari stacks
(``fn@file:line:col``). Each frame's file is mapped onto the repository by the longest suffix
of path segments it shares with one of the repository's files (``/app/backend/app/x.py`` is
``backend/app/x.py``); frames in libraries (``site-packages``, ``node_modules``, the runtime's
own) are kept but marked as outside the repository. The ticket says what the error is, where it
happened (the innermost frame in the repository first), and the versions the text mentions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field

Language = Literal["python", "javascript"]
MAX_TEXT = 200_000  # characters of a paste looked at
MAX_CONTEXTS = 3  # retries' added context kept, the latest
MAX_FRAMES = 200
MAX_LINE = 2_000  # characters: a longer line isn't a frame (and isn't matched)

_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<fn>.+))?$')
# The traceback's last line: the exception's (dotted) class, and its message.
_PY_ERROR = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<message>.*))?$")
# ``at fn (location:line:col)`` and ``at location:line:col``; Firefox/Safari ``fn@location:...``.
# A function name has no parentheses and a location no space, so the parts can't overlap (no
# backtracking blow-up on crafted lines); lines longer than MAX_LINE aren't frames anyway.
_JS_AT = re.compile(r"^\s*at (?P<fn>[^()]*?) \((?P<loc>\S+?):(?P<line>\d+)(?::\d+)?\)$")
_JS_AT_BARE = re.compile(r"^\s*at (?P<loc>\S+?):(?P<line>\d+)(?::\d+)?$")
_JS_AT_SIGN = re.compile(r"^\s*(?P<fn>[^@\s]*)@(?P<loc>\S+?):(?P<line>\d+)(?::\d+)?$")
_JS_ERROR = re.compile(
    r"^(?:Uncaught\s+(?:\(in promise\)\s+)?)?(?P<type>[A-Z]\w*(?:Error|Exception)|Error)"
    r"(?::\s?(?P<message>.*))?$"
)
_LIBRARY = re.compile(
    r"(^|/)(site-packages|dist-packages|node_modules|lib/python\d|\.venv|venv)(/|$)|"
    r"^node:|^internal/|^<|^\[native code\]$|^native$|^webpack-internal:///\(webpack\)"
)
_VERSION = re.compile(
    r"\b(?P<name>python|node|nodejs|django|fastapi|flask|react|next(?:\.js)?|express)"
    r"[ /@:v]*(?P<version>\d+(?:\.\d+){1,2})\b",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)


class Frame(BaseModel):
    file: str  # as the trace wrote it
    line: int
    function: str | None = None
    path: str | None = None  # the repository's file it is, when it is one
    in_repo: bool = False
    library: bool = False


class Ticket(BaseModel):
    """What a fix starts from."""

    title: str
    language: Language | None = None
    error_type: str | None = None
    message: str | None = None
    frames: list[Frame] = Field(default_factory=list[Frame])
    versions: dict[str, str] = Field(default_factory=dict[str, str])
    text: str = ""  # what was given, trimmed
    # What people added when trying it again (8.6), oldest first: "how to trigger it".
    context: list[str] = Field(default_factory=list[str])

    @property
    def culprit(self) -> Frame | None:
        """The innermost frame in the repository: where to start looking."""
        return next((f for f in reversed(self.frames) if f.in_repo), None)


# ---------------------------------------------------------------- paths


def _clean_location(location: str) -> str:
    """A frame's file as a plain path: a URL's path, ``file://`` and webpack prefixes off."""
    location = location.strip()
    if location.startswith(("webpack-internal:///", "webpack:///")):
        location = re.sub(r"^webpack(-internal)?:///(\(\w+\)/)?(\./)?", "", location)
    elif "://" in location:
        location = unquote(urlsplit(location).path)
    location = location.split("?", 1)[0]
    return location.replace("\\", "/")


class PathIndex:
    """The repository's files, to map a frame's path onto one."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._by_name: dict[str, list[tuple[str, ...]]] = {}
        for path in paths:
            parts = tuple(PurePosixPath(path).parts)
            if parts:
                self._by_name.setdefault(parts[-1], []).append(parts)

    def match(self, location: str) -> str | None:
        """The repository's file with the longest common suffix (one segment, the name alone,
        only if it's the only file of that name)."""
        parts = tuple(p for p in PurePosixPath(_clean_location(location)).parts if p != "/")
        if not parts:
            return None
        candidates = self._by_name.get(parts[-1], [])
        best: tuple[int, str] | None = None
        ambiguous = False
        for candidate in candidates:
            shared = 0
            for a, b in zip(reversed(parts), reversed(candidate), strict=False):
                if a != b:
                    break
                shared += 1
            if best is None or shared > best[0]:
                best, ambiguous = (shared, "/".join(candidate)), False
            elif shared == best[0]:
                ambiguous = True
        if best is None or ambiguous or (best[0] == 1 and len(candidates) > 1):
            return None
        return best[1]


def _frame(location: str, line: str, fn: str | None, index: PathIndex | None) -> Frame:
    cleaned = _clean_location(location)
    library = bool(_LIBRARY.search(cleaned)) or bool(_LIBRARY.search(location))
    path = None if library or index is None else index.match(location)
    return Frame(
        file=location, line=int(line), function=(fn or "").strip() or None, path=path,
        in_repo=path is not None, library=library,
    )  # fmt: skip


# ---------------------------------------------------------------- parsers


def _python(lines: Sequence[str], index: PathIndex | None) -> tuple[list[Frame], str, str] | None:
    """The frames of the last traceback, and its exception."""
    start = max((i for i, line in enumerate(lines) if line.startswith("Traceback (")), default=-1)
    if start < 0:
        return None
    frames: list[Frame] = []
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if len(line) > MAX_LINE:
            continue
        if found := _PY_FRAME.match(line):
            frames.append(_frame(found["file"], found["line"], found["fn"], index))
            continue
        if not line.startswith(" ") and (err := _PY_ERROR.match(line.strip())):
            return frames, err["type"], (err["message"] or "").strip()
    return (frames, "Exception", "") if frames else None


def _javascript(
    lines: Sequence[str], index: PathIndex | None
) -> tuple[list[Frame], str | None, str] | None:
    frames: list[Frame] = []
    error: tuple[str | None, str] | None = None
    for line in lines:
        if len(line) > MAX_LINE:
            continue
        stripped = line.strip()
        found = _JS_AT.match(line) or _JS_AT_BARE.match(line) or _JS_AT_SIGN.match(line)
        if found:
            frames.append(_frame(found["loc"], found["line"], found.groupdict().get("fn"), index))
        elif error is None and (err := _JS_ERROR.match(stripped)):
            error = (err["type"], (err["message"] or "").strip())
    if not frames:
        return None
    # JS stacks list the innermost call first; tickets keep the outermost first, as Python does.
    frames.reverse()
    kind, message = error or (None, "")
    return frames, kind, message


def _versions(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _VERSION.finditer(text):
        name = match["name"].lower().removesuffix(".js")
        found.setdefault("node" if name == "nodejs" else name, match["version"])
    return found


def _trace_text(text: str) -> str:
    """An issue's trace: its fenced code blocks if any (where traces are pasted), else all."""
    blocks = [b for b in _FENCE.findall(text) if "Traceback (" in b or re.search(r"\n\s*at ", b)]
    return "\n".join(blocks) if blocks else text


def parse_ticket(
    text: str,
    *,
    title: str | None = None,
    paths: Iterable[str] = (),
    index: PathIndex | None = None,
) -> Ticket:
    """A ticket from pasted text (a trace, or an issue's title and body); ``index`` (the
    repository's files, built once) or ``paths``."""
    text = text[:MAX_TEXT].replace("\r\n", "\n")
    index = index if index is not None else PathIndex(paths)
    lines = _trace_text(text).split("\n")
    parsed_py = _python(lines, index)
    language: Language | None = None
    frames: list[Frame] = []
    error_type: str | None = None
    message: str | None = None
    if parsed_py is not None:
        language = "python"
        frames, error_type, message = parsed_py
    elif (parsed_js := _javascript(lines, index)) is not None:
        language = "javascript"
        frames, error_type, message = parsed_js
    frames = frames[-MAX_FRAMES:]
    first_line = next((line.strip() for line in text.split("\n") if line.strip()), "")
    shown_error = f"{error_type}: {message}" if error_type and message else error_type
    return Ticket(
        title=(title or shown_error or first_line or "Untitled")[:200],
        language=language,
        error_type=error_type,
        message=message or None,
        frames=frames,
        versions=_versions(text),
        text=text.strip(),
    )
