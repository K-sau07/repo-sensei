"""Symbols: every definition in a parsed file, with a stable key, its span and what it says.

From a file's ``defs`` captures (:mod:`sensei_engine.parse`) come:

- the module itself (one per file);
- Python: classes, functions, methods, nested functions, class attributes, module variables and
  type aliases;
- JavaScript and TypeScript: functions (declarations and functions assigned to a name), classes
  and methods, fields, module variables, interfaces, type aliases, enums and namespaces;
  **components** (a capitalized function that renders JSX, a ``memo``/``forwardRef`` wrapper,
  a class extending ``Component``) and **hooks** (``use`` + a capital letter);
- Java: classes, interfaces, enums, records and annotation types at any depth (an inner class
  is keyed under its outer one), methods and constructors, fields (one per declarator), enum
  constants and record components. Java has no exports: visibility is in the signature.

``kind`` is one of :class:`SymbolKind` (what the API and ⌘K show); the finer kind (``field``,
``interface``, ``type``, ``enum``, ``record``, ``annotation``, ``namespace``, ``signature``,
``constructor``) is ``attrs["subkind"]``.
A symbol's lines span the whole definition: decorators (Java's annotations), ``export`` and the
``const`` a function is declared with included. ``signature`` is the source up to the body
(whitespace collapsed, decorators left out), ``docstring`` the docstring or the comment just
above (JSDoc and Javadoc, ``#`` or ``//`` lines; not a plain ``/* */`` in Java, where that is
usually a licence or code commented out). Columns count characters, from 0.
"""

from __future__ import annotations

import bisect
import enum
import hashlib
import inspect
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from tree_sitter import Node

from sensei_engine.parse.languages import COMMENTS, QueryKind
from sensei_engine.parse.parser import Match, ParseResult
from sensei_engine.symbols.keys import Descriptor, KeyAllocator, descriptor

MAX_SIGNATURE = 300
MAX_VALUE_SIGNATURE = 160  # variables and fields: their declaration, cut short
MAX_DOCSTRING = 2000
MAX_DECORATORS = 10
MAX_DECORATOR_TEXT = 200
MAX_BASES = 10


class SymbolKind(enum.StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    COMPONENT = "component"
    HOOK = "hook"
    VARIABLE = "variable"


# Query capture (definition.<x>) to kind, descriptor and subkind.
_DEFINITIONS: dict[str, tuple[SymbolKind, Descriptor, str | None]] = {
    "definition.class": (SymbolKind.CLASS, Descriptor.TYPE, None),
    "definition.function": (SymbolKind.FUNCTION, Descriptor.CALLABLE, None),
    "definition.method": (SymbolKind.METHOD, Descriptor.CALLABLE, None),
    "definition.component": (SymbolKind.COMPONENT, Descriptor.CALLABLE, None),
    "definition.hook": (SymbolKind.HOOK, Descriptor.CALLABLE, None),
    "definition.variable": (SymbolKind.VARIABLE, Descriptor.VALUE, None),
    "definition.field": (SymbolKind.VARIABLE, Descriptor.VALUE, "field"),
    "definition.interface": (SymbolKind.CLASS, Descriptor.TYPE, "interface"),
    "definition.type": (SymbolKind.CLASS, Descriptor.TYPE, "type"),
    "definition.enum": (SymbolKind.CLASS, Descriptor.TYPE, "enum"),
    "definition.record": (SymbolKind.CLASS, Descriptor.TYPE, "record"),
    "definition.annotation": (SymbolKind.CLASS, Descriptor.TYPE, "annotation"),
    "definition.constructor": (SymbolKind.METHOD, Descriptor.CALLABLE, "constructor"),
    "definition.module": (SymbolKind.MODULE, Descriptor.TYPE, "namespace"),
}
_JSX = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})
_REACT_BASES = re.compile(r"^(React\.)?(Pure)?Component$")
_BODY_FIELDS = ("body",)
_SIGNATURE_ONLY = frozenset({"function_signature", "method_signature", "abstract_method_signature"})
_WRAPPERS = frozenset({"export_statement", "ambient_declaration"})
_ANNOTATIONS = frozenset({"annotation", "marker_annotation"})
# Java declarations that hold their fields' declarators (private int a, b;).
_JAVA_DECLARATIONS = frozenset({"field_declaration", "constant_declaration"})
_SPACE = re.compile(r"\s+")
_OPEN_SPACE = re.compile(r"([(\[{]) ")
_CLOSE_SPACE = re.compile(r",? ?([)\]}])")


@dataclass(slots=True)
class Symbol:
    symbol_key: str
    kind: SymbolKind
    name: str
    qualified_name: str  # containers and name, joined with "."
    start_line: int  # 1-based, inclusive
    end_line: int
    start_col: int  # characters, 0-based
    end_col: int
    signature: str | None
    docstring: str | None
    content_hash: bytes  # SHA-256 of the definition's source
    attrs: dict[str, Any] = field(default_factory=dict[str, Any])
    container_key: str | None = None  # None: the module itself


@dataclass(slots=True)
class _Def:
    match: Match
    node: Node  # the definition node the query captured
    span: Node  # the node whose lines the symbol spans (with decorators, export, const)
    first: Node  # where the span starts (a decorator before a method, in a class body)
    name: str
    kind: SymbolKind
    suffix: Descriptor
    subkind: str | None
    container: _Def | None = None
    key: str = ""
    qualified: str = ""


def _collapse(text: str, limit: int) -> str:
    """One line: whitespace collapsed, none just inside brackets, no trailing commas."""
    text = _SPACE.sub(" ", text).strip()
    text = _OPEN_SPACE.sub(r"\1", text)
    text = _CLOSE_SPACE.sub(r"\1", text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _Source:
    def __init__(self, result: ParseResult) -> None:
        self.data = result.source
        self.lines = result.source.split(b"\n")
        self._char_starts: dict[int, list[int]] = {}

    def text(self, start: int, end: int) -> str:
        return self.data[start:end].decode("utf-8", "replace")

    def node(self, node: Node) -> str:
        return self.text(node.start_byte, node.end_byte)

    def code(self, root: Node, start: int, end: int) -> str:
        """The source from ``start`` to ``end`` (inside ``root``) without its comments."""
        cuts: list[tuple[int, int]] = []
        stack = [root]
        while stack:
            node = stack.pop()
            if node.end_byte <= start or node.start_byte >= end:
                continue
            if node.type in COMMENTS:
                cuts.append((max(node.start_byte, start), min(node.end_byte, end)))
                continue
            stack.extend(node.children)
        parts: list[str] = []
        position = start
        for cut_start, cut_end in sorted(cuts):
            parts.append(self.text(position, cut_start))
            position = cut_end
        parts.append(self.text(position, end))
        return " ".join(parts)

    def column(self, row: int, byte_column: int) -> int:
        """A byte column as a character column. Each line's character offsets are found once:
        a minified file has thousands of symbols on one line."""
        line = self.lines[row] if row < len(self.lines) else b""
        if line.isascii():
            return min(byte_column, len(line))
        starts = self._char_starts.get(row)
        if starts is None:
            # A character starts at every byte that isn't a UTF-8 continuation byte.
            starts = [i for i, byte in enumerate(line) if not 0x80 <= byte < 0xC0]
            self._char_starts[row] = starts
        return bisect.bisect_left(starts, byte_column)


def extract_symbols(result: ParseResult, language: str) -> list[Symbol]:
    """The symbols of a parsed file (``language``: the file's language id), in source order,
    the module first. A file that didn't parse has none."""
    if not result.ok or result.tree is None:
        return []
    return _Extractor(result, language).run(result.tree.root_node)


class _Extractor:
    def __init__(self, result: ParseResult, language: str) -> None:
        self.result = result
        self.language = language
        self.python = language == "python"
        self.jsx = language in ("javascript", "jsx", "tsx")
        self.java = language == "java"
        self.source = _Source(result)
        self.keys = KeyAllocator(language, result.path)

    def run(self, root: Node) -> list[Symbol]:
        matches = self.result.captures.get(QueryKind.DEFS, ())
        defs = [d for m in matches if (d := self._definition(m)) is not None]
        by_node = {d.node.id: d for d in defs}
        for d in defs:
            d.container = self._container(d, by_node)
        exports = self._exports(matches)
        module = self._module(root)
        symbols = [module]
        for d in defs:  # source order: a container's key exists before its children's
            parent_key = d.container.key if d.container else self.keys.module
            d.key = self.keys.allocate(parent_key, descriptor(d.name, d.suffix))
            d.qualified = f"{d.container.qualified}.{d.name}" if d.container else d.name
            symbols.append(self._symbol(d, exports))
        return symbols

    # ------------------------------------------------------------------ definitions

    def _definition(self, match: Match) -> _Def | None:
        found = _DEFINITIONS.get(match.kind)
        if found is None:
            return None  # export.* facts
        kind, suffix, subkind = found
        node = match.node
        name_node = match.role("name")
        name = self.source.node(name_node) if name_node is not None else "default"
        if kind is SymbolKind.COMPONENT and not self._renders_jsx(node):
            kind = SymbolKind.FUNCTION  # capitalized, but no JSX: a plain function
        if (
            kind is SymbolKind.FUNCTION
            and self.jsx
            and name_node is None
            and self._renders_jsx(node)
        ):
            kind = SymbolKind.COMPONENT  # export default () => <Page />
        if node.type in _SIGNATURE_ONLY:
            subkind = "signature"
        if kind is SymbolKind.CLASS and subkind is None and self._extends_react_component(node):
            kind, subkind = SymbolKind.COMPONENT, "class_component"
        span, first = self._span(node)
        return _Def(match, node, span, first, name, kind, suffix, subkind)

    def _renders_jsx(self, node: Node) -> bool:
        if not self.jsx:
            return False
        stack = list(node.children)
        while stack:
            child = stack.pop()
            if child.type in _JSX:
                return True
            stack.extend(child.children)
        return False

    def _extends_react_component(self, node: Node) -> bool:
        if not self.jsx:
            return False
        return any(_REACT_BASES.match(base) for base in self._bases(node))

    def _span(self, node: Node) -> tuple[Node, Node]:
        span = node
        parent = node.parent
        if self.python:
            if parent is not None and parent.type == "decorated_definition":
                span = parent
            elif node.type == "assignment" and parent is not None:
                span = parent  # the expression statement
            return span, span
        if self.java:
            # A field's annotations and modifiers are its declaration's: the field spans that,
            # unless the declaration has several (private int a, b;).
            if node.type == "variable_declarator" and parent is not None:
                declarators = parent.children_by_field_name("declarator")
                if parent.type in _JAVA_DECLARATIONS and len(declarators) == 1:
                    span = parent
            return span, span
        if (
            node.type == "variable_declarator"
            and parent is not None
            and parent.type in ("lexical_declaration", "variable_declaration")
        ):
            declarators = [c for c in parent.named_children if c.type == "variable_declarator"]
            if len(declarators) == 1:
                span = parent  # const f = ...: the const too
        if span.parent is not None and span.parent.type in _WRAPPERS:
            span = span.parent
        first = span
        previous = span.prev_named_sibling
        # A method's decorators precede it in the class body, comments maybe among them.
        while previous is not None and previous.type in ("decorator", "comment"):
            if previous.type == "decorator":
                first = previous
            previous = previous.prev_named_sibling
        return span, first

    @staticmethod
    def _statement(span: Node) -> Node:
        """The statement a span is part of: itself, or for one of several declarators
        (export const a = 1, b = 2) the statement around their declaration."""
        parent = span.parent
        if (
            span.type == "variable_declarator"
            and parent is not None
            and parent.type in ("lexical_declaration", "variable_declaration")
            and parent.parent is not None
            and parent.parent.type in _WRAPPERS
        ):
            return parent.parent
        return span

    def _container(self, d: _Def, by_node: dict[int, _Def]) -> _Def | None:
        parent = d.node.parent
        while parent is not None:
            found = by_node.get(parent.id)
            if found is not None:
                return found
            parent = parent.parent
        return None

    # ------------------------------------------------------------------ exports

    def _exports(self, matches: tuple[Match, ...]) -> dict[str, dict[str, Any]]:
        """Module-level names exported by name: export { a as b }, export default a, __all__."""
        found: dict[str, dict[str, Any]] = {}
        for match in matches:
            if match.kind == "export.all":
                name = match.role("name")
                if name is not None:
                    found.setdefault(self._string_value(name), {})["exported"] = True
            elif match.kind == "export" and (name := match.role("name")) is not None:
                entry = found.setdefault(self.source.node(name), {})
                entry["exported"] = True
                alias = match.role("alias")
                if alias is not None:
                    entry["export_alias"] = self.source.node(alias)
            elif match.kind == "export.default":
                value = match.role("value")
                if value is not None and value.type == "identifier":
                    entry = found.setdefault(self.source.node(value), {})
                    entry.update(exported=True, default_export=True)
        return found

    def _string_value(self, node: Node) -> str:
        parts = [c for c in node.named_children if c.type in ("string_content", "string_fragment")]
        return "".join(self.source.node(p) for p in parts)

    # ------------------------------------------------------------------ symbols

    def _module(self, root: Node) -> Symbol:
        path = PurePosixPath(self.result.path)
        package = path.stem in ("__init__", "index") and path.parent.name
        name = path.parent.name if package else path.stem
        last_line = max(1, root.end_point.row + (1 if root.end_point.column else 0))
        return Symbol(
            symbol_key=self.keys.module,
            kind=SymbolKind.MODULE,
            name=name or path.name,
            qualified_name=str(path.with_suffix("")).replace("/", "."),
            start_line=1,
            end_line=last_line,
            start_col=0,
            end_col=0,
            signature=None,
            docstring=self._python_docstring(root) if self.python else None,
            content_hash=hashlib.sha256(self.result.source).digest(),
            attrs={},
        )

    def _symbol(self, d: _Def, exports: dict[str, dict[str, Any]]) -> Symbol:
        first, span = d.first, d.span
        start_line = first.start_point.row + 1
        end_line = span.end_point.row + (1 if span.end_point.column else 0)
        attrs: dict[str, Any] = {}
        if d.subkind:
            attrs["subkind"] = d.subkind
        decorators = self._decorators(d)
        if decorators:
            attrs["decorators"] = decorators
        if any(c.type == "async" for c in self._callable(d.node).children):
            attrs["async"] = True
        bases = self._bases(d.node)
        if bases:
            attrs["bases"] = bases[:MAX_BASES]
        statement = self._statement(d.span)
        if not self.python and statement.type == "export_statement":
            attrs["exported"] = True
            if any(c.type == "default" for c in statement.children):
                attrs["default_export"] = True
        if d.container is None and d.name in exports:
            attrs.update(exports[d.name])
        content = self.source.data[first.start_byte : span.end_byte]
        return Symbol(
            symbol_key=d.key,
            kind=d.kind,
            name=d.name,
            qualified_name=d.qualified,
            start_line=start_line,
            end_line=max(start_line, end_line),
            start_col=self.source.column(first.start_point.row, first.start_point.column),
            end_col=self.source.column(span.end_point.row, span.end_point.column),
            signature=self._signature(d),
            docstring=self._docstring(d),
            content_hash=hashlib.sha256(content).digest(),
            attrs=attrs,
            container_key=d.container.key if d.container else self.keys.module,
        )

    def _callable(self, node: Node) -> Node:
        """The function node itself (for const f = async () => ...: the arrow function)."""
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type == "call_expression":  # memo(() => ...)
                arguments = value.child_by_field_name("arguments")
                if arguments is not None and arguments.named_children:
                    return arguments.named_children[0]
                return value
            return value if value is not None else node
        return node

    def _bases(self, node: Node) -> list[str]:
        if node.type == "class_definition":
            superclasses = node.child_by_field_name("superclasses")
            if superclasses is None:
                return []
            return [
                self.source.node(c)
                for c in superclasses.named_children
                if c.type != "keyword_argument"
            ]
        if self.java:
            return self._java_bases(node)
        found: list[str] = []
        for child in node.children:
            if child.type != "class_heritage":
                continue
            stack = list(child.children)
            while stack:
                part = stack.pop(0)
                if part.type in ("extends_clause", "implements_clause"):
                    stack[:0] = list(part.children)
                elif part.is_named and part.type not in ("type_arguments", "arguments"):
                    found.append(_collapse(self.source.node(part), 120))
        return found

    def _java_bases(self, node: Node) -> list[str]:
        """What a Java type extends, then what it implements, as written (JpaRepository<Order,
        Long>): a class's superclass and interfaces, an interface's extended interfaces, the
        interfaces of an enum or a record."""
        found: list[str] = []
        for child in node.children:
            if child.type == "superclass":
                types = child.named_children
            elif child.type in ("super_interfaces", "extends_interfaces"):
                types = [t for c in child.named_children for t in c.named_children]
            else:
                continue
            found.extend(_collapse(self.source.node(t), 120) for t in types)
        return found

    def _modifiers(self, node: Node) -> Node | None:
        """A Java declaration's modifiers (annotations and keywords); a field declarator's are
        its declaration's."""
        if node.type == "variable_declarator" and node.parent is not None:
            node = node.parent
        return next((c for c in node.named_children if c.type == "modifiers"), None)

    def _decorators(self, d: _Def) -> list[str]:
        nodes: list[Node] = []
        if self.java:
            modifiers = self._modifiers(d.node)
            if modifiers is not None:
                nodes = [c for c in modifiers.named_children if c.type in _ANNOTATIONS]
        elif self.python:
            if d.span.type == "decorated_definition":
                nodes = [c for c in d.span.named_children if c.type == "decorator"]
        else:
            sibling: Node | None = d.first
            while sibling is not None and sibling.type in ("decorator", "comment"):
                if sibling.type == "decorator":
                    nodes.append(sibling)
                sibling = sibling.next_named_sibling
            for holder in (d.span, d.node):
                nodes.extend(c for c in holder.named_children if c.type == "decorator")
        texts: list[str] = []
        for node in nodes:
            text = _collapse(
                self.source.code(node, node.start_byte, node.end_byte), MAX_DECORATOR_TEXT
            )
            if text not in texts:
                texts.append(text)
        return texts[:MAX_DECORATORS]

    # ------------------------------------------------------------------ signatures

    def _signature(self, d: _Def) -> str | None:
        node = d.node
        if d.kind is SymbolKind.VARIABLE or d.subkind == "type":
            return self._value_signature(d)
        if node.type == "variable_declarator":
            target = self._callable(node)
            body = target.child_by_field_name("body")
            end = body.start_byte if body is not None else target.end_byte
            keyword = self._declaration_keyword(node)
            text = self.source.code(node, node.start_byte, end)
            return _collapse(f"{keyword} {text}".strip(), MAX_SIGNATURE)
        body = next(
            (b for f in _BODY_FIELDS if (b := node.child_by_field_name(f)) is not None), None
        )
        start = self._after_decorators(node)
        end = body.start_byte if body is not None else node.end_byte
        text = self.source.code(node, start, end).rstrip().rstrip(":;").rstrip()
        return _collapse(text, MAX_SIGNATURE) or None

    def _after_decorators(self, node: Node) -> int:
        for child in node.children:
            if child.type == "modifiers":  # Java: the annotations lead the modifiers
                kept = next(
                    (m for m in child.children if m.type not in _ANNOTATIONS | COMMENTS), None
                )
                if kept is not None:
                    return kept.start_byte
                continue
            if child.type != "decorator":
                return child.start_byte
        return node.start_byte

    def _declaration_keyword(self, node: Node) -> str:
        parent = node.parent
        if parent is not None and parent.type in ("lexical_declaration", "variable_declaration"):
            first = parent.children[0] if parent.children else None
            if first is not None and not first.is_named:
                return self.source.node(first)
        return ""

    def _value_signature(self, d: _Def) -> str:
        node = d.node
        parent = node.parent
        if self.java and parent is not None and parent.type in _JAVA_DECLARATIONS:
            # The declaration's modifiers and type, then this declarator: private final
            # OrderService orderService; private int a = 1 and private int b for int a = 1, b.
            kind = parent.child_by_field_name("type")
            head_end = kind.end_byte if kind is not None else parent.start_byte
            head = self.source.code(parent, self._after_decorators(parent), head_end)
            text = f"{head} {self.source.code(node, node.start_byte, node.end_byte)}"
        elif self.java:
            # An enum constant (up to its class body) or a record component.
            body = node.child_by_field_name("body")
            end = body.start_byte if body is not None else node.end_byte
            text = self.source.code(node, self._after_decorators(node), end)
        elif node.type == "variable_declarator":
            code = self.source.code(node, node.start_byte, node.end_byte)
            text = f"{self._declaration_keyword(node)} {code}"
        else:
            text = self.source.code(node, self._after_decorators(node), node.end_byte)
        return _collapse(text.rstrip().rstrip(";"), MAX_VALUE_SIGNATURE)

    # ------------------------------------------------------------------ docstrings

    def _docstring(self, d: _Def) -> str | None:
        if self.python and d.node.type in ("function_definition", "class_definition"):
            body = d.node.child_by_field_name("body")
            text = self._python_docstring(body) if body is not None else None
            if text:
                return text
        anchor = d.first
        if self.java and anchor.type == "variable_declarator" and anchor.parent is not None:
            anchor = anchor.parent  # one of several declarators: the declaration's Javadoc
        return self._leading_comment(anchor)

    def _python_docstring(self, block: Node) -> str | None:
        first = next((c for c in block.named_children if c.type != "comment"), None)
        if first is None or first.type != "expression_statement" or first.named_child_count != 1:
            return None
        string = first.named_children[0]
        if string.type != "string" or any(c.type == "interpolation" for c in string.children):
            return None
        raw = "".join(
            self.source.node(c) for c in string.named_children if c.type == "string_content"
        )
        cleaned = inspect.cleandoc(raw)
        return cleaned[:MAX_DOCSTRING] or None

    def _starts_line(self, node: Node) -> bool:
        line = self.source.lines[node.start_point.row]
        return not line[: node.start_point.column].strip()

    def _leading_comment(self, node: Node) -> str | None:
        """The comments right above ``node`` (no blank line between), cleaned of their markers."""
        comments: list[Node] = []
        expected_row = node.start_point.row
        previous = node.prev_sibling
        while previous is not None and previous.type in COMMENTS:
            if previous.end_point.row < expected_row - 1 or not self._starts_line(previous):
                break  # a blank line between, or the end of the previous line's code
            raw = self.source.node(previous)
            if previous.type == "block_comment" and not raw.startswith("/**"):
                break  # Java: a licence or code commented out, not Javadoc
            comments.insert(0, previous)
            expected_row = previous.start_point.row
            previous = previous.prev_sibling
        if not comments:
            return None
        lines: list[str] = []
        for comment in comments:
            for line in self.source.node(comment).splitlines():
                stripped = line.strip()
                stripped = re.sub(r"^(/\*\*?|\*/|\*(?!/)|//+|#+)\s?", "", stripped)
                stripped = re.sub(r"\s*\*/$", "", stripped)
                lines.append(stripped)
        text = "\n".join(lines).strip()
        return text[:MAX_DOCSTRING] or None
