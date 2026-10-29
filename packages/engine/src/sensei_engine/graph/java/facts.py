"""Java's graph facts, read from a file's tree in the parse pool (3.18): what the resolvers of
:mod:`sensei_engine.graph.java` need and nothing that holds a tree.

- the file's **package** and **imports** (single-type, on-demand, static and static on-demand);
- each **type** (class, interface, enum, record, annotation type, at any depth), keyed as the
  symbols stage keys it, with what it extends and implements as written (``JpaRepository<Order,
  Long>``), its type parameters, annotations, **fields** with their declared types (an enum's
  constants, a record's components, the ``log`` Lombok's ``@Slf4j`` gives) and **methods** and
  constructors: parameters with their types, the return type, annotations, and the fields a
  constructor assigns its parameters to (``this.repo = repo``);
- each callable's **locals**, with the lines they are seen on: ``Type x = ...``, ``var x = new
  Type()`` (``var x = call()``: the call, its return type is ``x``'s), enhanced-for, catch and
  try-with-resources variables, ``instanceof`` patterns and lambda parameters (of unknown type
  unless declared);
- every **call** (the ``calls`` query's captures: ``a.m()``, ``m()``, ``new T()``, ``this(...)``/
  ``super(...)``, ``a::m``, ``T::new``) with its receiver (a name chain, ``this``, ``super``,
  another call's result, a ``new`` or a cast), what each argument looks like (for overloads),
  and where it is: the innermost definition (the edge's source), the callable and the type;
- the simple type names the file uses (what an on-demand import's edges go to).

Positions are 1-based lines. Definitions are found by the symbols stage's spans (the innermost
symbol around a definition's name), so keys are the ones its nodes have.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from tree_sitter import Node

from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

TypeKind = Literal["class", "interface", "enum", "record", "annotation"]
CallKind = Literal["method", "new", "this", "super", "reference", "reference_new"]
ReceiverKind = Literal["none", "this", "super", "name", "call", "new", "cast", "dead", "other"]
ImportKind = Literal["single", "wildcard", "static", "static_wildcard"]

_TYPES: dict[str, TypeKind] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
_CALLABLES = frozenset({
    "method_declaration", "constructor_declaration", "compact_constructor_declaration",
    "annotation_type_element_declaration",
})  # fmt: skip
_ANNOTATIONS = frozenset({"annotation", "marker_annotation"})
_CALL_KINDS: dict[str, CallKind] = {
    "call.method": "method",
    "call": "method",
    "call.new": "new",
    "call.reference": "reference",
    "call.reference.new": "reference_new",
}
# What a literal argument is (an overload's parameter it fits: :mod:`.calls`).
_LITERALS = {
    "string_literal": "String",
    "character_literal": "char",
    "decimal_integer_literal": "int",
    "hex_integer_literal": "int",
    "octal_integer_literal": "int",
    "binary_integer_literal": "int",
    "decimal_floating_point_literal": "double",
    "hex_floating_point_literal": "double",
    "true": "boolean",
    "false": "boolean",
    "null_literal": "null",
}
# Lombok's loggers: the field each annotation adds, and its type.
LOMBOK_LOGGERS = {
    "Slf4j": "org.slf4j.Logger",
    "XSlf4j": "org.slf4j.ext.XLogger",
    "Log4j2": "org.apache.logging.log4j.Logger",
    "Log4j": "org.apache.log4j.Logger",
    "Log": "java.util.logging.Logger",
    "CommonsLog": "org.apache.commons.logging.Log",
    "JBossLog": "org.jboss.logging.Logger",
    "Flogger": "com.google.common.flogger.FluentLogger",
}
MAX_CHAIN = 8


@dataclass(frozen=True, slots=True)
class Annotation:
    name: str  # as written: "GetMapping", "org.springframework.lang.Nullable"
    arguments: str | None  # its argument list's source ('("/{id}")'), None for a marker
    line: int


@dataclass(frozen=True, slots=True)
class Variable:
    """A field, a parameter or a local variable."""

    name: str
    type: str | None  # as written ("List<Order>", "Order[]"); None: not told (var, a lambda's)
    line: int
    end_line: int = 0  # a local: the last line it is seen on
    key: str | None = None  # a field's symbol key
    annotations: tuple[Annotation, ...] = ()
    static: bool = False
    final: bool = False
    initialized: bool = False  # a field with an initializer (Lombok's constructors skip it)
    value: int = -1  # var x = <call>: the call's index (its return type is x's)
    owner: str | None = None  # a local's callable (or type, in an initializer): its key


@dataclass(frozen=True, slots=True)
class Method:
    key: str
    name: str
    line: int
    end_line: int
    constructor: bool
    parameters: tuple[Variable, ...] = ()
    varargs: bool = False
    returns: str | None = None  # as written; None for a constructor
    static: bool = False
    annotations: tuple[Annotation, ...] = ()
    type_parameters: tuple[str, ...] = ()
    assigns: tuple[tuple[str, str], ...] = ()  # (field, parameter): this.repo = repo


@dataclass(frozen=True, slots=True)
class JavaType:
    key: str
    name: str
    qualified: str  # with the types around it in the file: "Order.Line"
    kind: TypeKind
    line: int
    end_line: int
    extends: tuple[str, ...] = ()  # as written, type arguments kept
    implements: tuple[str, ...] = ()
    type_parameters: tuple[str, ...] = ()
    annotations: tuple[Annotation, ...] = ()
    fields: tuple[Variable, ...] = ()
    methods: tuple[Method, ...] = ()
    outer: str | None = None  # the type it is declared in
    local: bool = False  # declared in a callable (not importable)
    static: bool = False


@dataclass(frozen=True, slots=True)
class Receiver:
    """What a call is made on. ``name``: a name chain (``orderService``, ``this.repo``,
    ``Outer.Inner``, ``com.acme.Util``); ``call``: the result of call ``call``; ``new`` and
    ``cast``: an instance of the type ``parts[0]``; ``dead``: a literal, a class literal, an
    array (no method the graph has); ``other``: an expression of unknown type."""

    kind: ReceiverKind
    parts: tuple[str, ...] = ()
    call: int = -1


@dataclass(frozen=True, slots=True)
class Call:
    line: int
    kind: CallKind
    name: str  # the method; the type for new and reference_new; "this"/"super" for those
    receiver: Receiver
    arguments: tuple[str, ...] | None  # each one's look (see _argument); None: a reference
    source: str  # the innermost definition around it (the edge's source)
    callable: str | None  # the callable around it (its parameters and locals)
    owner: str | None  # the type around it
    text: str = ""  # the receiver as written (cut to 120 characters)
    static: bool = False  # Type.m(): on a capitalized name (a type's, by convention)
    column: int = 0  # the name's (bytes), with ``line``: two calls on a line told apart


@dataclass(frozen=True, slots=True)
class JavaImport:
    line: int
    kind: ImportKind
    source: str  # the package (single, wildcard), the class (static), as written
    name: str  # the imported name, "*" for an on-demand import

    @property
    def target(self) -> str:
        return self.source if self.name == "*" else f"{self.source}.{self.name}"


@dataclass(frozen=True, slots=True)
class JavaFacts:
    module: str  # the file's module key
    package: str  # "" in the default package
    imports: tuple[JavaImport, ...] = ()
    types: tuple[JavaType, ...] = ()
    calls: tuple[Call, ...] = ()
    locals: tuple[Variable, ...] = ()
    type_names: frozenset[str] = field(default_factory=frozenset[str])


# ---------------------------------------------------------------------- reading the tree


def _text(node: Node | None) -> str:
    raw = node.text if node is not None else None
    return " ".join((raw or b"").decode("utf-8", "replace").split())


def _line(node: Node) -> int:
    return node.start_point.row + 1


def _end_line(node: Node) -> int:
    return node.end_point.row + 1


def _modifiers(node: Node) -> Node | None:
    return next((c for c in node.children if c.type == "modifiers"), None)


def _keywords(node: Node) -> set[str]:
    modifiers = _modifiers(node)
    return {c.type for c in modifiers.children if not c.is_named} if modifiers else set()


def annotations_of(node: Node) -> tuple[Annotation, ...]:
    """A declaration's annotations (in its modifiers)."""
    modifiers = _modifiers(node)
    found: list[Annotation] = []
    for child in modifiers.named_children if modifiers else ():
        if child.type in _ANNOTATIONS:
            arguments = child.child_by_field_name("arguments")
            found.append(
                Annotation(
                    _text(child.child_by_field_name("name")),
                    _text(arguments) if arguments is not None else None,
                    _line(child),
                )
            )
    return tuple(found)


def _type_parameters(node: Node) -> tuple[str, ...]:
    parameters = node.child_by_field_name("type_parameters")
    if parameters is None:
        return ()
    names: list[str] = []
    for parameter in parameters.named_children:
        name = next((c for c in parameter.named_children if c.type == "type_identifier"), None)
        if name is not None:
            names.append(_text(name))
    return tuple(names)


def _bases(node: Node) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What a type declaration extends and implements, as written."""
    extends: list[str] = []
    implements: list[str] = []
    for child in node.children:
        if child.type == "superclass":
            extends.extend(_text(t) for t in child.named_children)
        elif child.type == "extends_interfaces":  # an interface's
            extends.extend(_text(t) for c in child.named_children for t in c.named_children)
        elif child.type == "super_interfaces":
            implements.extend(_text(t) for c in child.named_children for t in c.named_children)
    return tuple(extends), tuple(implements)


class _Keys:
    """The symbols stage's key of the innermost definition around a position."""

    def __init__(self, result: ParseResult, symbols: Sequence[Symbol]) -> None:
        self._lines = result.source.split(b"\n")
        self.module = next(s.symbol_key for s in symbols if s.kind is SymbolKind.MODULE)
        spans = [s for s in symbols if s.kind is not SymbolKind.MODULE]
        self._spans = sorted(
            spans, key=lambda s: (s.start_line, s.start_col, -s.end_line, -s.end_col)
        )
        # Where each span starts, and the span around it (-1: none): a lookup bisects to the
        # last span starting by the point and climbs out, rather than scanning every span.
        self._starts = [(s.start_line, s.start_col) for s in self._spans]
        self._parents: list[int] = []
        open_: list[int] = []
        for at, span in enumerate(self._spans):
            start = (span.start_line, span.start_col)
            while open_ and not start < self._end(open_[-1]):
                open_.pop()
            self._parents.append(open_[-1] if open_ else -1)
            open_.append(at)

    def _end(self, at: int) -> tuple[int, int]:
        span = self._spans[at]
        return span.end_line, span.end_col

    def _point(self, node: Node) -> tuple[int, int]:
        row, byte_column = node.start_point
        line = self._lines[row] if row < len(self._lines) else b""
        return row + 1, len(line[:byte_column].decode("utf-8", "replace"))

    def around(self, node: Node) -> str:
        point = self._point(node)
        at = bisect.bisect_right(self._starts, point) - 1
        while at >= 0 and not point < self._end(at):
            at = self._parents[at]
        return self._spans[at].symbol_key if at >= 0 else self.module


@dataclass(slots=True)
class _Type:
    key: str
    name: str
    qualified: str
    kind: TypeKind
    node: Node
    outer: str | None
    local: bool
    fields: list[Variable] = field(default_factory=list[Variable])
    methods: list[Method] = field(default_factory=list[Method])

    def freeze(self) -> JavaType:
        extends, implements = _bases(self.node)
        annotations = annotations_of(self.node)
        fields = list(self.fields)
        for annotation in annotations:  # @Slf4j: a log field
            logger = LOMBOK_LOGGERS.get(annotation.name.rsplit(".", 1)[-1])
            if logger is not None and not any(f.name == "log" for f in fields):
                fields.append(Variable("log", logger, annotation.line, static=True, final=True))
        return JavaType(
            self.key,
            self.name,
            self.qualified,
            self.kind,
            _line(self.node),
            _end_line(self.node),
            extends,
            implements,
            _type_parameters(self.node),
            annotations,
            tuple(fields),
            tuple(self.methods),
            self.outer,
            self.local,
            "static" in _keywords(self.node) or self.kind != "class",
        )


class _Walker:
    """Types, members and locals, in one walk of the tree."""

    def __init__(self, result: ParseResult, keys: _Keys) -> None:
        self.result = result
        self.keys = keys
        self.types: list[_Type] = []
        self.locals: list[Variable] = []
        self.type_names: set[str] = set()
        self.callables: dict[tuple[int, int], str] = {}  # a callable's node: its key
        self.type_nodes: dict[tuple[int, int], str] = {}
        self.calls: dict[tuple[int, int], int] = {}  # a call node: its index (a var's value)

    def run(self, root: Node) -> None:
        # (node, the type it is a member of (None: in an anonymous class or a callable's
        # body, not a member), the type around it, the callable around it)
        stack: list[tuple[Node, _Type | None, _Type | None, str | None]] = [
            (root, None, None, None)
        ]
        while stack:
            node, member_of, around, callable_key = stack.pop()
            kind = node.type
            if kind in _TYPES:
                created = self._type(node, around, callable_key)
                body = node.child_by_field_name("body")
                stack.extend(self._members(node, body, created))
                continue
            if kind in _CALLABLES:
                key = self._callable(node, member_of)
                stack.extend((c, None, around, key) for c in reversed(node.named_children))
                continue
            if kind in ("field_declaration", "constant_declaration") and member_of is not None:
                self._fields(node, member_of)
            elif kind == "enum_constant" and member_of is not None:
                self._enum_constant(node, member_of)
            elif kind == "class_body" and node.parent is not None and node.parent.type in (
                "object_creation_expression", "enum_constant",
            ):  # fmt: skip
                member_of = None  # an anonymous class's members are no type's
            elif callable_key is not None or around is not None:
                self._local(node, callable_key or (around.key if around else None))
            if kind in ("type_identifier", "scoped_type_identifier") or (
                kind == "identifier" and (node.text or b"")[:1].isupper()  # Money.format(), @Entity
            ):
                self.type_names.add(_text(node).split(".", 1)[0])
            stack.extend((c, member_of, around, callable_key) for c in reversed(node.children))

    def _members(
        self, node: Node, body: Node | None, created: _Type
    ) -> Iterator[tuple[Node, _Type | None, _Type | None, str | None]]:
        """A type declaration's children to walk: its body's are its members."""
        if node.type == "record_declaration":
            self._record_components(node, created)
        for child in reversed(node.children):
            if body is not None and child.id == body.id:
                yield from ((c, created, created, None) for c in reversed(child.named_children))
            else:
                yield child, None, created, None

    def _type(self, node: Node, around: _Type | None, callable_key: str | None) -> _Type:
        name = _text(node.child_by_field_name("name"))
        key = self.keys.around(node.child_by_field_name("name") or node)
        qualified = f"{around.qualified}.{name}" if around is not None else name
        created = _Type(
            key, name, qualified, _TYPES[node.type], node,
            around.key if around else None, callable_key is not None,
        )  # fmt: skip
        self.types.append(created)
        self.type_nodes[(node.start_byte, node.end_byte)] = key
        return created

    def _callable(self, node: Node, member_of: _Type | None) -> str:
        name_node = node.child_by_field_name("name")
        key = self.keys.around(name_node or node)
        self.callables[(node.start_byte, node.end_byte)] = key
        if member_of is None:  # an anonymous class's method: its parameters are its locals
            parameters, _ = self._parameters(node.child_by_field_name("parameters"))
            self.locals.extend(
                Variable(p.name, p.type, p.line, _end_line(node), owner=key) for p in parameters
            )
            return key
        constructor = node.type in ("constructor_declaration", "compact_constructor_declaration")
        parameters, varargs = self._parameters(node.child_by_field_name("parameters"))
        if node.type == "compact_constructor_declaration":
            parameters = tuple(f for f in member_of.fields if not f.static)
        returns = node.child_by_field_name("type")
        member_of.methods.append(
            Method(
                key,
                _text(name_node),
                _line(node),
                _end_line(node),
                constructor,
                parameters,
                varargs,
                _text(returns) if returns is not None and not constructor else None,
                "static" in _keywords(node),
                annotations_of(node),
                _type_parameters(node),
                _assigns(node.child_by_field_name("body")) if constructor else (),
            )
        )
        return key

    def _parameters(self, node: Node | None) -> tuple[tuple[Variable, ...], bool]:
        found: list[Variable] = []
        varargs = False
        for parameter in node.named_children if node is not None else ():
            if parameter.type == "formal_parameter":
                name = parameter.child_by_field_name("name")
                kind = parameter.child_by_field_name("type")
                dimensions = parameter.child_by_field_name("dimensions")
                written = _text(kind) + (_text(dimensions) if dimensions is not None else "")
            elif parameter.type == "spread_parameter":
                varargs = True
                kind = next((c for c in parameter.named_children if c.type != "modifiers"), None)
                declarator = next(
                    (c for c in parameter.named_children if c.type == "variable_declarator"), None
                )
                name = declarator.child_by_field_name("name") if declarator else None
                written = f"{_text(kind)}[]"
            else:
                continue  # a receiver parameter (Foo this)
            if name is not None:
                found.append(
                    Variable(
                        _text(name),
                        written,
                        _line(parameter),
                        annotations=annotations_of(parameter),
                        final="final" in _keywords(parameter),
                    )
                )
        return tuple(found), varargs

    def _fields(self, node: Node, member_of: _Type) -> None:
        kind = node.child_by_field_name("type")
        keywords = _keywords(node)
        interface = member_of.kind in ("interface", "annotation")
        annotations = annotations_of(node)
        for declarator in node.children_by_field_name("declarator"):
            name = declarator.child_by_field_name("name")
            dimensions = declarator.child_by_field_name("dimensions")
            written = _text(kind) + (_text(dimensions) if dimensions is not None else "")
            member_of.fields.append(
                Variable(
                    _text(name),
                    written,
                    _line(node),
                    key=self.keys.around(name or declarator),
                    annotations=annotations,
                    static="static" in keywords or interface,
                    final="final" in keywords or interface,
                    initialized=declarator.child_by_field_name("value") is not None,
                )
            )

    def _enum_constant(self, node: Node, member_of: _Type) -> None:
        name = node.child_by_field_name("name")
        member_of.fields.append(
            Variable(
                _text(name),
                member_of.name,
                _line(node),
                key=self.keys.around(name or node),
                annotations=annotations_of(node),
                static=True,
                final=True,
                initialized=True,
            )
        )

    def _record_components(self, node: Node, member_of: _Type) -> None:
        parameters = node.child_by_field_name("parameters")
        for parameter in parameters.named_children if parameters is not None else ():
            name = parameter.child_by_field_name("name")
            kind = parameter.child_by_field_name("type")
            if name is None:
                continue
            member_of.fields.append(
                Variable(
                    _text(name),
                    _text(kind),
                    _line(parameter),
                    key=self.keys.around(name),
                    annotations=annotations_of(parameter),
                    final=True,
                )
            )

    # ------------------------------------------------------------------ locals

    def _local(self, node: Node, owner: str | None) -> None:
        kind = node.type
        if kind == "local_variable_declaration":
            self._declaration(node, owner)
        elif kind == "enhanced_for_statement":
            self._add(
                node.child_by_field_name("name"), node.child_by_field_name("type"), node, owner
            )
        elif kind == "catch_formal_parameter":
            catch_type = next((c for c in node.named_children if c.type == "catch_type"), None)
            types = catch_type.named_children if catch_type is not None else []
            scope = node.parent or node
            # catch (A | B e): of neither type alone (unknown); catch (A e): an A
            written = types[0] if len(types) == 1 else None
            self._add(node.child_by_field_name("name"), written, scope, owner)
        elif kind == "resource":
            scope = node.parent.parent if node.parent and node.parent.parent else node
            self._add(
                node.child_by_field_name("name"), node.child_by_field_name("type"), scope, owner
            )
        elif kind == "instanceof_expression":
            scope = _block(node)
            self._add(
                node.child_by_field_name("name"), node.child_by_field_name("right"), scope, owner
            )
        elif kind == "lambda_expression":
            self._lambda(node, owner)

    def _declaration(self, node: Node, owner: str | None) -> None:
        kind = node.child_by_field_name("type")
        scope = node.parent or node
        for declarator in node.children_by_field_name("declarator"):
            name = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if _text(kind) != "var":
                self._add(name, kind, scope, owner, start=node)
                continue
            # var x = new T(...) / (T) y: a T; var x = call(): what the call returns
            told: str | None = None
            call = -1
            if value is not None and value.type == "object_creation_expression":
                told = _created_type(value)
            elif value is not None and value.type == "cast_expression":
                told = _text(value.child_by_field_name("type"))
            elif value is not None and value.type == "method_invocation":
                call = self.result_index(value)
            self._add(name, told, scope, owner, start=node, value=call)

    def _lambda(self, node: Node, owner: str | None) -> None:
        parameters = node.child_by_field_name("parameters")
        if parameters is None:
            return
        if parameters.type == "identifier":
            self._add(parameters, None, node, owner)
            return
        for parameter in parameters.named_children:
            if parameter.type == "identifier":  # (a, b) -> ...
                self._add(parameter, None, node, owner)
            elif parameter.type == "formal_parameter":
                kind = parameter.child_by_field_name("type")
                told = kind if kind is not None and _text(kind) != "var" else None
                self._add(parameter.child_by_field_name("name"), told, node, owner)

    def _add(
        self,
        name: Node | None,
        kind: Node | str | None,
        scope: Node,
        owner: str | None,
        *,
        start: Node | None = None,
        value: int = -1,
    ) -> None:
        if name is None:
            return
        written = kind if isinstance(kind, str) or kind is None else _text(kind)
        if written == "var":  # for (var x : xs): not told
            written = None
        self.locals.append(
            Variable(
                _text(name),
                written,
                _line(start or name),
                _end_line(scope),
                value=value,
                owner=owner,
            )
        )

    def result_index(self, node: Node) -> int:
        return self.calls.get((node.start_byte, node.end_byte), -1)


def _block(node: Node) -> Node:
    """The block (or the statement, at most the callable) a node is in."""
    current = node.parent
    while current is not None and current.type not in ("block", "constructor_body", "switch_block"):
        if current.type in _CALLABLES or current.type == "lambda_expression":
            return current
        current = current.parent
    return current or node


def _assigns(body: Node | None) -> tuple[tuple[str, str], ...]:
    """``this.x = y`` (and ``x = y``) statements of a constructor's body: (field, value)."""
    found: list[tuple[str, str]] = []
    for statement in body.named_children if body is not None else ():
        expression = statement.named_children[0] if statement.named_children else None
        if statement.type != "expression_statement" or expression is None:
            continue
        if expression.type != "assignment_expression":
            continue
        left = expression.child_by_field_name("left")
        right = expression.child_by_field_name("right")
        if left is None or right is None or right.type != "identifier":
            continue
        if left.type == "field_access" and _text(left.child_by_field_name("object")) == "this":
            found.append((_text(left.child_by_field_name("field")), _text(right)))
        elif left.type == "identifier":
            found.append((_text(left), _text(right)))
    return tuple(found)


def _created_type(node: Node) -> str:
    """The type ``new T<...>(...)`` creates (or ``(T) x`` casts to), its type arguments kept."""
    return _text(node.child_by_field_name("type"))


# ---------------------------------------------------------------------- calls


def _chain(node: Node | None) -> tuple[str, ...] | None:
    """``a.b.c`` (identifiers and field accesses, ``this`` among them) as its parts."""
    parts: list[str] = []
    while node is not None and node.type == "field_access":
        field_node = node.child_by_field_name("field")
        parts.append("this" if field_node is None else _text(field_node))
        node = node.child_by_field_name("object")
    if node is None or len(parts) >= MAX_CHAIN:
        return None
    if node.type in ("identifier", "this", "scoped_identifier", "type_identifier"):
        parts.extend(reversed(_text(node).split(".")))
    elif node.type == "super":
        parts.append("super")
    else:
        return None
    return tuple(reversed(parts))


def _field_this(node: Node) -> bool:
    """``Outer.this`` (a field access whose field is the keyword)."""
    return node.type == "field_access" and any(c.type == "this" for c in node.children)


_DEAD_RECEIVERS = frozenset({
    *_LITERALS, "class_literal", "array_creation_expression", "array_access",
})  # fmt: skip
_TYPE_RECEIVERS = frozenset({"generic_type", "scoped_type_identifier", "array_type"})


def receiver_of(node: Node | None, calls: dict[tuple[int, int], int]) -> Receiver:
    """What a call's receiver is (see :class:`Receiver`)."""
    while node is not None and node.type == "parenthesized_expression":
        node = node.named_children[0] if node.named_children else None
    if node is None:
        return Receiver("none")
    kind = node.type
    if kind in ("this", "super"):
        return Receiver("this" if kind == "this" else "super")
    if kind == "method_invocation":
        return Receiver("call", call=calls.get((node.start_byte, node.end_byte), -1))
    if kind in ("object_creation_expression", "cast_expression"):
        written = _created_type(node)
        return Receiver("new" if kind == "object_creation_expression" else "cast", (written,))
    if kind in _DEAD_RECEIVERS:
        return Receiver("dead")
    return _named_receiver(node)


def _named_receiver(node: Node) -> Receiver:
    """A receiver that is a name: a variable, a field, a type (``T::m``), ``Outer.this``."""
    if _field_this(node):  # Outer.this
        outer = _chain(node.child_by_field_name("object"))
        return Receiver("name", (*outer, "this")) if outer else Receiver("other")
    if node.type in _TYPE_RECEIVERS:  # List<X>::m, Outer.Inner::m
        return Receiver("name", tuple(_text(node).split("<", 1)[0].split(".")))
    chain = _chain(node) if node.type in ("identifier", "field_access", "type_identifier") else None
    return Receiver("name", chain) if chain else Receiver("other")


def _argument(node: Node) -> str:
    """What an argument looks like, for picking an overload: ``=<type>`` a type it surely has
    (a literal's, ``new T()``, a cast), ``$<name>`` a name whose type its declaration tells,
    ``""`` unknown."""
    while node.type == "parenthesized_expression" and node.named_children:
        node = node.named_children[0]
    literal = _LITERALS.get(node.type)
    if literal is not None:
        suffix = _text(node)[-1:].lower()
        if (literal, suffix) in (("int", "l"), ("double", "f")):  # 3L, 1.5f (not 0xFF)
            literal = "long" if suffix == "l" else "float"
        return f"={literal}"
    if node.type in ("object_creation_expression", "cast_expression"):
        return f"={_created_type(node)}"
    if node.type == "identifier":
        return f"${_text(node)}"
    if node.type == "field_access" and _text(node.child_by_field_name("object")) == "this":
        return f"$this.{_text(node.child_by_field_name('field'))}"
    return ""


def _static_looking(receiver: Receiver) -> bool:
    first = receiver.parts[0] if receiver.kind == "name" and receiver.parts else ""
    return first[:1].isupper() and not first.isupper()


def _arguments(node: Node | None) -> tuple[str, ...]:
    return tuple(
        _argument(a)
        for a in (node.named_children if node is not None else ())
        if a.type not in ("line_comment", "block_comment")
    )


def _context(node: Node, walker: _Walker) -> tuple[str | None, str | None]:
    """The callable and the (named) type a call is in, by its ancestors."""
    callable_key: str | None = None
    current = node.parent
    while current is not None:
        span = (current.start_byte, current.end_byte)
        if callable_key is None and (current.type in _CALLABLES):
            callable_key = walker.callables.get(span)
        if current.type in _TYPES:
            owner = walker.type_nodes.get(span)
            return callable_key or owner, owner
        current = current.parent
    return callable_key, None


_CallMatch = tuple[Node, str, dict[str, Node | None]]


def _call_matches(result: ParseResult) -> list[_CallMatch]:
    """The calls query's captures, one per call node (the first pattern's), in order."""
    ordered: list[_CallMatch] = []
    seen: set[tuple[int, int]] = set()
    for match in result.captures.get(QueryKind.CALLS, ()):
        span = (match.node.start_byte, match.node.end_byte)
        if span not in seen and (match.kind in _CALL_KINDS or match.kind == "call.constructor"):
            seen.add(span)
            roles = {r: match.role(r) for r in ("name", "receiver", "arguments")}
            ordered.append((match.node, match.kind, roles))
    return ordered


def _calls(ordered: list[_CallMatch], walker: _Walker) -> list[Call]:
    index = walker.calls
    found: list[Call] = []
    for node, capture, roles in ordered:  # one Call each: receivers name them by index
        name = _text(roles["name"])
        kind: CallKind = _CALL_KINDS.get(capture) or ("this" if name == "this" else "super")
        if kind in ("new", "reference_new"):
            name = name.split("<", 1)[0]
        receiver = (
            receiver_of(roles["receiver"], index)
            if kind in ("method", "reference")
            else Receiver("none")
        )
        callable_key, owner = _context(node, walker)
        name_node = roles["name"]
        found.append(
            Call(
                # The name's line: a chain's calls each on their own (a.b()\n .c()).
                _line(name_node if name_node is not None and kind == "method" else node),
                kind,
                name,
                receiver,
                None if kind.startswith("reference") else _arguments(roles["arguments"]),
                walker.keys.around(node),
                callable_key,
                owner,
                _text(roles["receiver"])[:120] if roles["receiver"] is not None else "",
                _static_looking(receiver),
                (name_node if name_node is not None and kind == "method" else node).start_point[1],
            )
        )
    return found


# ---------------------------------------------------------------------- imports


def _imports(result: ParseResult) -> tuple[str, list[JavaImport]]:
    package = ""
    found: list[JavaImport] = []
    seen: set[tuple[int, int]] = set()
    kinds: dict[str, ImportKind] = {
        "import": "single",
        "import.wildcard": "wildcard",
        "import.static": "static",
        "import.static.wildcard": "static_wildcard",
    }
    for match in result.captures.get(QueryKind.IMPORTS, ()):
        if match.kind == "import.package":
            package = package or _text(match.role("name"))
            continue
        span = (match.node.start_byte, match.node.end_byte)
        kind = kinds.get(match.kind)
        source = match.role("source")
        if kind is None or source is None or span in seen:
            continue
        seen.add(span)
        name = "*" if kind.endswith("wildcard") else _text(match.role("name"))
        found.append(JavaImport(_line(match.node), kind, _text(source).replace(" ", ""), name))
    return package, found


def extract(result: ParseResult) -> JavaFacts | None:
    """A Java file's facts (see the module); None when it didn't parse."""
    if not result.ok or result.tree is None:
        return None
    symbols = extract_symbols(result, "java")
    if not symbols:
        return None
    keys = _Keys(result, symbols)
    walker = _Walker(result, keys)
    package, imports = _imports(result)
    matches = _call_matches(result)  # numbered first: a var's value names its call
    walker.calls = {(n.start_byte, n.end_byte): i for i, (n, _, _) in enumerate(matches)}
    walker.run(result.tree.root_node)
    calls = _calls(matches, walker)
    return JavaFacts(
        keys.module,
        package,
        tuple(imports),
        tuple(t.freeze() for t in walker.types),
        tuple(calls),
        tuple(walker.locals),
        frozenset(walker.type_names),
    )
