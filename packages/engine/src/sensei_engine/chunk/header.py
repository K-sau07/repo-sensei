"""The context header on every chunk: where it is and what it is, in a few short lines.

```
# path: backend/app/api/routes/items.py
# lang: python · function: read_items · exports: read_items
# signature: def read_items(session: SessionDep, skip: int = 0) -> Any
# doc: Retrieve items.
# decorators: @router.get("/", response_model=ItemsPublic)
# uses: SessionDep, select, func
# part: 2/3
```

``uses`` lists the file's imported names that the chunk's own content mentions. Every field is
cut short, so a header stays well under a hundred tokens.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

MAX_FIELD = 200  # characters
MAX_DOC = 160
MAX_NAMES = 12
MAX_DECORATORS = 3
MAX_DECORATOR = 80

_IDENTIFIER = re.compile(r"[^\W\d][\w$]*")
_SPACE = re.compile(r"\s+")


def _short(text: str, limit: int = MAX_FIELD) -> str:
    text = _SPACE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:MAX_NAMES])
    return shown + (f" (+{len(names) - MAX_NAMES})" if len(names) > MAX_NAMES else "")


@dataclass(frozen=True, slots=True)
class Header:
    path: str
    language: str
    what: tuple[str, str] | None = None  # ("function", "read_items"), ("section", "Install")
    container: str | None = None  # the enclosing class or component
    exports: str | None = None
    signature: str | None = None
    doc: str | None = None  # the docstring (its first line is shown)
    decorators: Sequence[str] = ()
    defines: Sequence[str] = ()  # the definitions a chunk of several holds
    imported: Sequence[str] = ()  # the file's imported names (those the content uses are shown)
    extra: Sequence[tuple[str, str]] = ()  # more fields, as they are

    def render(self, content: str, part: tuple[int, int] | None = None) -> str:
        lines = [f"# path: {self.path}"]
        about = [f"lang: {self.language}"]
        if self.what is not None:
            about.append(f"{self.what[0]}: {_short(self.what[1])}")
        if self.container:
            about.append(f"in: {_short(self.container)}")
        if self.exports:
            about.append(f"exports: {_short(self.exports)}")
        lines.append("# " + " · ".join(about))
        if self.signature:
            lines.append(f"# signature: {_short(self.signature)}")
        doc = next((line for line in (self.doc or "").splitlines() if line.strip()), None)
        if doc:
            lines.append(f"# doc: {_short(doc, MAX_DOC)}")
        if self.decorators:
            shown = [_short(d, MAX_DECORATOR) for d in self.decorators[:MAX_DECORATORS]]
            lines.append(f"# decorators: {' '.join(shown)}")
        if self.defines:
            lines.append(f"# defines: {_names(self.defines)}")
        uses = self.uses(content)
        if uses:
            lines.append(f"# uses: {_names(uses)}")
        lines.extend(f"# {name}: {_short(value)}" for name, value in self.extra)
        if part is not None:
            lines.append(f"# part: {part[0]}/{part[1]}")
        return "\n".join(lines)

    def uses(self, content: str) -> list[str]:
        """The imported names ``content`` mentions, in the order it first does."""
        if not self.imported:
            return []
        imported = set(self.imported)
        found: list[str] = []
        for token in _IDENTIFIER.findall(content):
            if token in imported and token not in found:
                found.append(token)
        return found
