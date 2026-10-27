"""Small readers of Python syntax trees (tree-sitter) the linkers share."""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator

from tree_sitter import Node

from sensei_engine.parse.parser import ParseResult

_ESCAPE_RE = re.compile(
    r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{1,3}|.)", re.DOTALL
)


def walk(root: Node) -> Iterator[Node]:
    """Every node under ``root`` (itself first), in source order."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.named_children))


def ancestors(node: Node) -> Iterator[Node]:
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def line(node: Node) -> int:
    return node.start_point.row + 1


def _unescape(found: re.Match[str]) -> str:
    """One backslash escape in a string literal, decoded as Python does."""
    try:
        return codecs.decode("\\" + found[1], "unicode_escape")
    except UnicodeDecodeError:
        return found[1]


def string_value(result: ParseResult, node: Node | None) -> str | None:
    """A plain string literal's value (adjacent literals joined); None for anything else, an
    f-string with a replacement field included."""
    if node is None:
        return None
    if node.type == "concatenated_string":
        parts = [string_value(result, child) for child in node.named_children]
        return None if any(part is None for part in parts) else "".join(p or "" for p in parts)
    if node.type != "string":
        return None
    start = node.named_children[0] if node.named_children else None
    raw = start is not None and "r" in result.text(start).lower()
    value: list[str] = []
    for child in node.named_children:
        if child.type == "interpolation":
            return None
        if child.type == "string_content":
            content = result.text(child)
            value.append(content if raw else _ESCAPE_RE.sub(_unescape, content))
    return "".join(value)


def call_args(node: Node) -> tuple[list[Node], dict[str, Node]]:
    """A call's positional arguments and keyword arguments (by name)."""
    arguments = node.child_by_field_name("arguments")
    positional: list[Node] = []
    keywords: dict[str, Node] = {}
    if arguments is None or arguments.type != "argument_list":
        return positional, keywords
    for arg in arguments.named_children:
        if arg.type == "comment":
            continue
        if arg.type == "keyword_argument":
            key, value = arg.child_by_field_name("name"), arg.child_by_field_name("value")
            if key is not None and value is not None and key.text is not None:
                keywords[key.text.decode("utf-8", "replace")] = value
        else:
            positional.append(arg)
    return positional, keywords
