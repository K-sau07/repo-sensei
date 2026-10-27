"""A JavaScript or TypeScript file's imports and exports, read from its parse captures.

:func:`extract_imports` turns the ``imports`` captures into one :class:`ImportStatement` per
binding, in source order:

- ``import X from "m"``: ``X`` bound to ``m``'s ``default``;
- ``import { a as b } from "m"``: ``b`` bound to ``a``;
- ``import * as ns from "m"``, ``import fs = require("fs")``, ``const m = require("m")``: the
  whole module (``*``);
- ``const { a, b: c } = require("m")``: named, as an import;
- ``export { a as b } from "m"``, ``export * as ns from "m"``: re-exports, binding nothing here
  but exported from here (``local`` is the exported name);
- ``export * from "m"``: every name ``m`` exports but ``default``;
- ``import "m"``, ``import("m")``, a ``require`` not assigned to a name: the module, no name.

Type-only imports and exports (``import type``, ``{ type T }``) are marked. A specifier that
isn't a literal (``import(`./${name}`)``) can't be resolved and is left out.

:func:`extract_exports` gives the names exported without a source (``export { a as b }``,
``export default a``, ``export default memo(a)``): a barrel that imports a name and exports it
again, a chain the resolver follows (:mod:`sensei_engine.imports.ecma.table`).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from tree_sitter import Node

from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import Match, ParseResult

NAMESPACE = "*"  # the whole module, as a name
DEFAULT = "default"


class ImportKind(enum.StrEnum):
    DEFAULT = "default"
    NAMED = "named"
    NAMESPACE = "namespace"
    REQUIRE = "require"
    REEXPORT = "reexport"  # export { a as b } from "m"; export * as ns from "m"
    REEXPORT_ALL = "reexport_all"  # export * from "m"
    MODULE = "module"  # import "m", export {} from "m"
    DYNAMIC = "dynamic"  # import("m")


@dataclass(frozen=True, slots=True)
class ImportStatement:
    kind: ImportKind
    source: str  # the specifier, as written
    line: int  # 1-based
    imported: str | None = None  # the name taken from the module: a name, "default" or "*"
    local: str | None = None  # the name bound here; for a re-export, the name exported
    type_only: bool = False


@dataclass(frozen=True, slots=True)
class LocalExport:
    """``export { local as exported }`` or ``export default local``, with no source."""

    local: str
    exported: str
    line: int


Names = list[tuple[str | None, str | None]]  # (imported, local) pairs

_BINDINGS = {
    "import.default": ImportKind.DEFAULT,
    "import.named": ImportKind.NAMED,
    "import.namespace": ImportKind.NAMESPACE,
    "import.require": ImportKind.REQUIRE,
    "import.reexport": ImportKind.REEXPORT,
    "import.dynamic": ImportKind.DYNAMIC,
}


def literal(result: ParseResult, node: Node) -> str | None:
    """The value of a string or of a template string with no substitutions; None otherwise."""
    if node.type == "template_string":
        if any(child.type == "template_substitution" for child in node.named_children):
            return None
    elif node.type != "string":
        return None
    return result.text(node)[1:-1]


def _typed(node: Node | None) -> bool:
    return node is not None and any(child.type == "type" for child in node.children)


def _line(node: Node) -> int:
    return node.start_point.row + 1


def _required_names(result: ParseResult, call: Node) -> Names:
    """What ``require(...)`` is assigned to: ``[("*", m)]`` for ``const m = require(...)``,
    the names of ``const { a, b: c } = ...``, nothing for a bare call."""
    parent = call.parent
    if parent is None or parent.type != "variable_declarator":
        return []
    target = parent.child_by_field_name("name")
    if target is None:
        return []
    if target.type == "identifier":
        return [(NAMESPACE, result.text(target))]
    if target.type != "object_pattern":
        return []
    names: Names = []
    for child in target.named_children:
        if child.type == "shorthand_property_identifier_pattern":
            names.append((result.text(child), result.text(child)))
        elif child.type == "pair_pattern":
            key, value = child.child_by_field_name("key"), child.child_by_field_name("value")
            if key is not None and value is not None and value.type == "identifier":
                names.append((result.text(key), result.text(value)))
    return names


def _default_names(result: ParseResult, match: Match) -> Names:
    local = match.role("default")
    return [(DEFAULT, result.text(local))] if local is not None else []


def _named_names(result: ParseResult, match: Match) -> Names:
    name, alias = match.role("name"), match.role("alias")
    if name is None:
        return []
    return [(result.text(name), result.text(alias if alias is not None else name))]


def _namespace_names(result: ParseResult, match: Match) -> Names:
    local = match.role("namespace")
    return [(NAMESPACE, result.text(local))] if local is not None else []


def _require_names(result: ParseResult, match: Match) -> Names:
    local = match.role("default")  # import fs = require("fs")
    if local is not None:
        return [(NAMESPACE, result.text(local))]
    return list(_required_names(result, match.node)) or [(None, None)]


def _reexport_names(result: ParseResult, match: Match) -> Names:
    """A name or a namespace re-exported; ``(*, None)``: ``export * from``."""
    return _named_names(result, match) or _namespace_names(result, match) or [(NAMESPACE, None)]


def _module_names(result: ParseResult, match: Match) -> Names:
    return [(None, None)]


_NAMES = {
    ImportKind.DEFAULT: _default_names,
    ImportKind.NAMED: _named_names,
    ImportKind.NAMESPACE: _namespace_names,
    ImportKind.REQUIRE: _require_names,
    ImportKind.REEXPORT: _reexport_names,
    ImportKind.DYNAMIC: _module_names,
}


def _binding(result: ParseResult, match: Match, source: str) -> list[ImportStatement]:
    kind = _BINDINGS[match.kind]
    name = match.role("name")
    type_only = _typed(match.node) or (name is not None and _typed(name.parent))
    line = _line(match.node)
    statements: list[ImportStatement] = []
    for imported, local in _NAMES[kind](result, match):
        actual = ImportKind.REEXPORT_ALL if kind is ImportKind.REEXPORT and not local else kind
        statements.append(ImportStatement(actual, source, line, imported, local, type_only))
    return statements


def extract_imports(result: ParseResult) -> list[ImportStatement]:
    """Every import binding of the file, in source order (see the module)."""
    found: list[ImportStatement] = []
    bound: set[tuple[int, int]] = set()  # statements with a binding: their module match is dropped
    modules: list[tuple[Match, str]] = []
    for match in result.captures.get(QueryKind.IMPORTS, ()):
        node = match.role("source")
        source = literal(result, node) if node is not None else None
        if source is None or not source.strip():
            continue
        if match.kind == "import.module":
            modules.append((match, source))
            continue
        statements = _binding(result, match, source)
        if statements:
            bound.add((match.node.start_byte, match.node.end_byte))
            found.extend(statements)
    for match, source in modules:
        if (match.node.start_byte, match.node.end_byte) not in bound:
            found.append(
                ImportStatement(
                    ImportKind.MODULE, source, _line(match.node), type_only=_typed(match.node)
                )
            )
    return sorted(found, key=lambda s: s.line)


def extract_exports(result: ParseResult) -> list[LocalExport]:
    """Names exported with no source: ``export { a as b }``, ``export default a``."""
    found: list[LocalExport] = []
    for match in result.captures.get(QueryKind.DEFS, ()):
        if match.kind == "export" and (name := match.role("name")) is not None:
            alias = match.role("alias")
            exported = result.text(alias if alias is not None else name)
            found.append(LocalExport(result.text(name), exported, _line(match.node)))
        elif match.kind == "export.default":
            value = _wrapped(match.role("value"))
            if value is not None and value.type == "identifier":
                found.append(LocalExport(result.text(value), DEFAULT, _line(match.node)))
    return found


def _wrapped(value: Node | None) -> Node | None:
    """What ``export default`` exports: ``memo(Page)``, ``observer(forwardRef(Page))`` export
    ``Page``, wrapped (the call's first argument, through nested wrappers)."""
    while value is not None and value.type == "call_expression":
        arguments = value.child_by_field_name("arguments")
        value = arguments.named_children[0] if arguments and arguments.named_children else None
    return value
