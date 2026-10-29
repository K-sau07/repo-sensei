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
  constants and record components. Java has no exports: visibility is in the signature;
- Kotlin: classes, interfaces, data classes, enums, annotation classes, objects and companion
  objects (``Companion`` when unnamed), functions (methods in a class or object body) and
  secondary constructors (named after their class, as Java's are), properties (a primary
  constructor's ``val``/``var`` parameters among them), enum entries, type aliases;
- Go: the package, struct and interface types (and other named types), functions, methods keyed
  under their receiver's type (``Server#Handle().``, the type maybe declared in another file of
  the package), struct fields, interface methods, package-level constants and variables;
- C#: namespaces, classes, interfaces, structs, records, enums and delegates, methods, local
  functions and constructors, properties, fields and events (one per declarator), enum members
  and a record's positional parameters;
- PHP: namespaces, classes, interfaces, traits and enums, methods (``__construct`` a
  constructor) and functions, properties (constructor-promoted ones among them), class
  constants and enum cases;
- Ruby: modules and classes, nested (``class Api::V1::Orders`` keyed as if nested), instance
  methods, class methods (``def self.x`` and those in ``class << self``), constants;
- Rust: modules, structs, enums, unions, traits and type aliases, functions, an ``impl``'s
  functions and constants keyed under its type (``impl Repo for PgRepo``: ``PgRepo#find().``),
  a trait's methods, fields and enum variants, constants and statics, ``macro_rules!``;
- C: functions (named through pointer and parenthesized declarators: ``int *(*f(int))(void)``
  is ``f``), prototypes (subkind ``signature``), structs, unions and enums with their fields
  and enumerators, typedefs (``typedef struct { ... } point_t;`` is the struct ``point_t``),
  globals (one per declarator; a function's locals aren't symbols), macros (a function-like
  one a callable, one with a value a constant; an include guard's isn't one);
- C++: what C has, and namespaces, classes and structs (a template's name without its
  arguments), methods defined or declared in a class body (constructors, destructors
  ``~Box``, operators ``operator==``), methods defined outside their class keyed under it
  (``void Box::run() {}``: ``Box#run().``, a namespace the file declares or opens left out of
  ``acme::Box::run``), ``using`` aliases, lambdas held in a variable (functions).

A C#, PHP or C++ namespace is a symbol but not a container: a class is keyed by its file and
name alike in a file-scoped and a block namespace, as Java's are without their package. A C or
C++ function declared and defined in the same file (a prototype above its definition, a method
declared in its class and defined below it; so too an extern variable, a static member) is one
symbol, the definition; one declared in a header and defined in a source file is a symbol in
each (see :mod:`sensei_engine.symbols.keys`).

``kind`` is one of :class:`SymbolKind` (what the API and ⌘K show); the finer kind (``field``,
``interface``, ``type``, ``enum``, ``record``, ``annotation``, ``namespace``, ``signature``,
``constructor``, ``destructor``, ``struct``, ``trait``, ``union``, ``object``, ``companion``,
``data_class``, ``property``, ``classmethod``, ``macro``, ``package``, ``module``) is
``attrs["subkind"]``.
A symbol's lines span the whole definition: decorators (Java's and Kotlin's annotations, C#'s,
PHP's and Rust's attributes), ``export``, the ``const`` a function is declared with and a C++
``template <...>`` included.
``signature`` is the source up to the body (whitespace collapsed, decorators left out),
``docstring`` the docstring or the comment just above (JSDoc, Javadoc, KDoc, PHPDoc, C#'s
``///`` XML (its ``<summary>`` tags dropped), Rust's ``///``; ``#`` or ``//`` lines; not a plain
``/* */`` outside JavaScript, TypeScript, C and C++ (where it is how functions are documented),
as elsewhere that is usually a licence or code commented out, nor Rust's ``//!``, which
documents the module it is in). ``bases`` are what a type extends and implements, as written:
superclasses and interfaces, Kotlin's delegation specifiers, Go's embedded types, PHP's traits,
Ruby's included modules, a Rust trait's supertraits and the traits a type implements in its
file, a C++ class's base classes. Columns count characters, from 0.
"""

from __future__ import annotations

import bisect
import enum
import hashlib
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from tree_sitter import Node

from sensei_engine.parse.languages import COMMENTS, QueryKind
from sensei_engine.parse.parser import Match, ParseResult, lines
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
    "definition.package": (SymbolKind.MODULE, Descriptor.TYPE, "package"),
    # A Rust mod or a Ruby module: a scope that holds definitions.
    "definition.mod": (SymbolKind.MODULE, Descriptor.TYPE, "module"),
    "definition.struct": (SymbolKind.CLASS, Descriptor.TYPE, "struct"),
    "definition.trait": (SymbolKind.CLASS, Descriptor.TYPE, "trait"),
    "definition.union": (SymbolKind.CLASS, Descriptor.TYPE, "union"),
    "definition.object": (SymbolKind.CLASS, Descriptor.TYPE, "object"),
    "definition.companion": (SymbolKind.CLASS, Descriptor.TYPE, "companion"),
    "definition.data_class": (SymbolKind.CLASS, Descriptor.TYPE, "data_class"),
    "definition.property": (SymbolKind.VARIABLE, Descriptor.VALUE, "property"),
    "definition.classmethod": (SymbolKind.METHOD, Descriptor.CALLABLE, "classmethod"),
    "definition.macro": (SymbolKind.FUNCTION, Descriptor.CALLABLE, "macro"),
    # A C object-like macro with a value (#define MAX_CONN 64).
    "definition.macro_value": (SymbolKind.VARIABLE, Descriptor.VALUE, "macro"),
}
# The name of a definition the source leaves unnamed.
_UNNAMED = {"definition.companion": "Companion"}
_JSX = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})
_REACT_BASES = re.compile(r"^(React\.)?(Pure)?Component$")
_BODY_FIELDS = ("body",)
_SIGNATURE_ONLY = frozenset({"function_signature", "method_signature", "abstract_method_signature"})
_WRAPPERS = frozenset({"export_statement", "ambient_declaration"})
_ANNOTATIONS = frozenset({"annotation", "marker_annotation"})
# Declarations that hold their declarators (private int a, b;), by declarator: a declarator's
# annotations, modifiers and docstring are its declaration's. Java's and PHP's are its children,
# C#'s are in its variable_declaration, Go's specs in a group's list.
_HOLDERS: dict[str, frozenset[str]] = {
    "variable_declarator": frozenset(
        {"field_declaration", "constant_declaration", "event_field_declaration"}
    ),
    "property_element": frozenset({"property_declaration"}),
    "const_element": frozenset({"const_declaration"}),
    "const_spec": frozenset({"const_declaration"}),
    "var_spec": frozenset({"var_declaration"}),
    "type_spec": frozenset({"type_declaration"}),
    "type_alias": frozenset({"type_declaration"}),
}
_DECLARATOR_LISTS = frozenset({"variable_declaration", "var_spec_list"})
# Kotlin's bodies (it names none ``body``): a class's, an enum's, a function's (``= expr`` among
# them) and a secondary constructor's.
_BODIES = frozenset({"class_body", "enum_class_body", "function_body", "block"})
_ACCESSORS = frozenset({"getter", "setter"})
# Attribute nodes a declaration holds before its modifiers (C#, and PHP's ``attributes``).
_ATTRIBUTE_LISTS = frozenset({"attribute_list"})
# Rust's attributes and doc comments stand before the item, as its siblings.
_RUST_PREFIX = frozenset({"attribute_item", "line_comment", "block_comment"})
_ECMA = frozenset({"javascript", "jsx", "typescript", "tsx"})
_XML_DOC_TAGS = re.compile(r"</?(?:summary|remarks)>")
_RUBY_MIXINS = frozenset({"include", "prepend"})
_ASYNC = frozenset({"async", "suspend"})
_MODIFIER_NODES = frozenset({"modifiers", "modifier", "function_modifiers", "function_modifier"})
# C and C++: the declarations that hold declarators (int a, *b;), and the declarators a name is
# seen through without changing what it declares (int (f)(void), int x = 1); a pointer, a
# reference or an array to what is inside them does (int (*f)(void) is a variable).
_C_HOLDERS = frozenset({"declaration", "field_declaration", "type_definition"})
_C_TRANSPARENT = frozenset({"parenthesized_declarator", "init_declarator", "attributed_declarator"})
# The names a declarator ends in (the C grammar reads uint8_t and size_t as primitive types:
# typedef unsigned char uint8_t;).
_C_NAMES = frozenset({"identifier", "field_identifier", "type_identifier", "primitive_type"})
_C_TYPES = frozenset({"struct_specifier", "union_specifier", "enum_specifier"})
_C_BODIED = _C_TYPES | {"class_specifier", "function_definition"}
# A declarator's children that aren't the declarator it holds (int *const p).
_C_QUALIFIERS = frozenset(
    {"type_qualifier", "ms_pointer_modifier", "ms_based_modifier", "attribute_specifier"}
)
_C_LOCAL_SCOPES = frozenset({"compound_statement", "function_definition"})
_OPERATOR_SPACE = re.compile(r" ?([^\w ]) ?")
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
    owner: tuple[str, ...] = ()  # the type it is keyed under (a Go receiver's, an impl's)
    # A C or C++ function's parameter types (names and defaults left out: (int,char*)const),
    # and whether this is only its declaration (a prototype, a method declared in its class).
    parameters: str | None = None
    declaration: bool = False
    container: _Def | None = None
    key: str = ""
    qualified: str = ""


@dataclass(slots=True)
class _Declared:
    """What a C or C++ declarator declares: its name (``operator==``, ``~Box``, ``f`` for
    ``f<int>``), the scopes it is qualified with (``Box`` in ``Box::run``), the node naming it,
    and the function declarator whose parameters it has if it declares a function."""

    name: str
    scopes: tuple[str, ...]
    node: Node
    function: Node | None


def _declared(node: Node, text: Callable[[Node], str]) -> _Declared | None:
    """The name a C or C++ declarator declares, through any nesting of pointers, arrays,
    parentheses and function declarators (``int *(*handlers(int))(void)`` declares the function
    ``handlers``; ``int (*handler)(void)`` a variable)."""
    function: Node | None = None
    scopes: list[str] = []
    while True:
        kind = node.type
        if kind in _C_NAMES:
            return _Declared(text(node), tuple(scopes), node, function)
        if kind in ("destructor_name", "operator_name"):
            name = _OPERATOR_SPACE.sub(r"\1", _SPACE.sub(" ", text(node)))
            return _Declared(name, tuple(scopes), node, function)
        if kind == "operator_cast":  # operator bool() const: its own parameters
            cast = node.child_by_field_name("type")
            name = f"operator {_collapse(text(cast), 80)}" if cast is not None else text(node)
            return _Declared(name, tuple(scopes), node, node.child_by_field_name("declarator"))
        if kind == "template_function":  # a specialization: template <> void f<int>(int)
            inner = node.child_by_field_name("name")
        elif kind == "qualified_identifier":
            scope = node.child_by_field_name("scope")
            if scope is not None:
                named = scope.child_by_field_name("name") if scope.type == "template_type" else None
                scopes.append(text(named if named is not None else scope))
            inner = node.child_by_field_name("name")
        else:
            if kind == "function_declarator":
                function = node
            elif kind not in _C_TRANSPARENT:
                function = None  # a pointer or an array of it: a variable
            inner = node.child_by_field_name("declarator") or next(
                (c for c in node.named_children if c.type not in _C_QUALIFIERS), None
            )
        if inner is None:
            return None
        node = inner


def _rust_type_name(node: Node) -> Node | None:
    """A Rust type's own name, without its path or type arguments (Order for crate::Order<T>)."""
    while node.type in ("generic_type", "scoped_type_identifier"):
        inner = node.child_by_field_name("type" if node.type == "generic_type" else "name")
        if inner is None:
            return None
        node = inner
    return node if node.type == "type_identifier" else None


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
        self.kotlin = language == "kotlin"
        self.go = language == "go"
        self.csharp = language == "csharp"
        self.php = language == "php"
        self.ruby = language == "ruby"
        self.rust = language == "rust"
        self.cpp = language == "cpp"
        self.c_family = language in ("c", "cpp")
        self.jvm = self.java or self.kotlin
        self.ecma = language in _ECMA
        self.source = _Source(result)
        self.keys = KeyAllocator(language, result.path)
        self.root: Node | None = None
        self._implemented: dict[str, list[Node]] | None = None
        self._namespaces: frozenset[str] | None = None

    def run(self, root: Node) -> list[Symbol]:
        self.root = root
        matches = self.result.captures.get(QueryKind.DEFS, ())
        defs = [d for m in matches if (d := self._definition(m)) is not None]
        by_node = {d.node.id: d for d in defs}
        for d in defs:
            d.container = self._container(d, by_node)
        if self.c_family:
            defs = self._c_defined(defs)
        exports = self._exports(matches)
        module = self._module(root)
        symbols = [module]
        for d in defs:  # source order: a container's key exists before its children's
            parent_key = d.container.key if d.container else self.keys.module
            parent_key += "".join(descriptor(o, Descriptor.TYPE) for o in d.owner)
            d.key = self.keys.allocate(parent_key, descriptor(d.name, d.suffix))
            names = [d.container.qualified] if d.container else []
            d.qualified = ".".join([*names, *d.owner, d.name])
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
        if name_node is None and self.c_family:
            return self._c_definition(match, kind, suffix, subkind)
        if name_node is not None:
            name = self.source.node(name_node)
        else:
            name = _UNNAMED.get(match.kind, "default")
        owner_node = match.role("owner")
        owner = self._owner(owner_node) if owner_node is not None else ()
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
        return _Def(match, node, span, first, name, kind, suffix, subkind, owner)

    def _c_definition(
        self, match: Match, kind: SymbolKind, suffix: Descriptor, subkind: str | None
    ) -> _Def | None:
        """A C or C++ definition named by its declarator (a function's, or one of a
        declaration's): a function or a variable by what the declarator declares, keyed under
        the class a qualified name is in (``Box::run``). A function's locals (its variables and
        typedefs; not a lambda it holds in one), and a typedef that only names the struct it
        declares, are none."""
        node = match.node
        defined = node.type == "function_definition"
        declarator = node.child_by_field_name("declarator") if defined else node
        found = _declared(declarator, self.source.node) if declarator is not None else None
        if found is None:
            return None
        if match.kind in ("definition.variable", "definition.type") and self._local(node):
            return None
        if match.kind == "definition.type" and self._names_its_type(node, found.name):
            return None
        value = node.child_by_field_name("value") if node.type == "init_declarator" else None
        held = value is not None and value.type == "lambda_expression"
        if match.kind == "definition.type":
            pass  # typedef int handler_fn(int); names a function type
        elif found.function is not None or defined:
            if kind is SymbolKind.VARIABLE:
                kind = SymbolKind.METHOD if subkind == "field" else SymbolKind.FUNCTION
            suffix, subkind = Descriptor.CALLABLE, None if defined else "signature"
        elif kind in (SymbolKind.FUNCTION, SymbolKind.METHOD) and not held:
            # A declaration in a class body that declares no function: a field.
            kind, suffix, subkind = SymbolKind.VARIABLE, Descriptor.VALUE, "field"
        owner = self._unqualified(found.scopes)
        if owner and kind is SymbolKind.FUNCTION:
            kind = SymbolKind.METHOD
        elif not owner and kind is SymbolKind.METHOD and found.scopes:
            kind = SymbolKind.FUNCTION  # void acme::helper() {}: a namespace's function
        elif owner and kind is SymbolKind.VARIABLE:
            subkind = "field"  # int Box::count = 0; a static member's definition
        if found.function is not None:
            declaration = not defined
        else:
            declaration = kind is SymbolKind.VARIABLE and self._declares_only(node)
        span, first = self._span(node)
        return _Def(
            match,
            node,
            span,
            first,
            found.name,
            kind,
            suffix,
            subkind,
            owner,
            parameters=self._parameters(found.function) if found.function is not None else None,
            declaration=declaration,
        )

    def _declares_only(self, declarator: Node) -> bool:
        """A variable declared, not defined: extern int count; and a C++ class's static
        int count; without a value (defined as int Box::count = 0;)."""
        holder = self._holder(declarator)
        if holder is None or declarator.type == "init_declarator":
            return False
        storage = {
            self.source.node(c) for c in holder.children if c.type == "storage_class_specifier"
        }
        if holder.type == "declaration":
            return "extern" in storage
        static = holder.type == "field_declaration" and "static" in storage
        return static and holder.child_by_field_name("default_value") is None

    @staticmethod
    def _local(node: Node) -> bool:
        """In a function's body (or a K&R function's parameter declarations)."""
        parent = node.parent
        while parent is not None:
            if parent.type in _C_LOCAL_SCOPES:
                return True
            parent = parent.parent
        return False

    def _names_its_type(self, declarator: Node, name: str) -> bool:
        """A typedef's name that is its struct's, union's or enum's: the first one of an
        anonymous type (typedef struct { ... } point_t;, the struct point_t), or the type's own
        tag (typedef struct node { ... } node;)."""
        holder = declarator.parent
        kind = holder.child_by_field_name("type") if holder is not None else None
        if holder is None or kind is None or kind.type not in _C_TYPES:
            return False
        if kind.child_by_field_name("body") is None:
            return False
        tag = kind.child_by_field_name("name")
        if tag is None:
            first = holder.child_by_field_name("declarator")
            return first is not None and first.id == declarator.id
        return self.source.node(tag) == name

    def _unqualified(self, scopes: tuple[str, ...]) -> tuple[str, ...]:
        """A qualified name's scopes but the namespaces the file declares or opens with using
        namespace, leading them (acme in acme::Box::run): what a definition is keyed under."""
        if not scopes:
            return ()
        namespaces = self._file_namespaces()
        start = 0
        while start < len(scopes) and scopes[start] in namespaces:
            start += 1
        return scopes[start:]

    def _file_namespaces(self) -> frozenset[str]:
        """The names of the namespaces a C++ file declares (each part of namespace a::b) or
        opens (using namespace a::b), outside functions, found once."""
        if self._namespaces is None:
            names: set[str] = set()
            stack = [self.root] if self.root is not None and self.cpp else []
            while stack:
                node = stack.pop()
                if node.type == "namespace_definition":
                    name = node.child_by_field_name("name")
                    if name is not None:
                        names.update(self.source.node(name).replace(" ", "").split("::"))
                elif node.type == "using_declaration" and "namespace" in (
                    c.type for c in node.children
                ):
                    target = node.named_children[-1] if node.named_children else None
                    if target is not None:
                        names.update(self.source.node(target).replace(" ", "").split("::"))
                    continue
                if node.type not in _C_LOCAL_SCOPES:
                    stack.extend(node.named_children)
            self._namespaces = frozenset(names)
        return self._namespaces

    def _parameters(self, function: Node) -> str:
        """A function's parameter types, its parameters' names and default values left out,
        whitespace dropped (``(int,char*)const`` for ``(int n, char *name = 0) const``): what a
        declaration and its definition share."""
        parameters = function.child_by_field_name("parameters")
        types: list[str] = []
        for parameter in parameters.named_children if parameters is not None else ():
            if parameter.type in COMMENTS:
                continue
            start, end = parameter.start_byte, parameter.end_byte
            declarator = parameter.child_by_field_name("declarator")
            if declarator is not None and parameter.child_by_field_name("default_value"):
                end = declarator.end_byte
            data = self.source.data[start:end]
            found = _declared(declarator, self.source.node) if declarator is not None else None
            if found is not None and found.node.type == "identifier":
                cut = (found.node.start_byte - start, found.node.end_byte - start)
                data = data[: cut[0]] + data[cut[1] :]
            types.append(_SPACE.sub("", data.decode("utf-8", "replace")))
        if types == ["void"]:
            types = []  # f(void) is f()
        qualifiers = "".join(
            self.source.node(c) for c in function.children if c.type == "type_qualifier"
        )
        return f"({','.join(types)}){qualifiers}"

    def _c_defined(self, defs: list[_Def]) -> list[_Def]:
        """C and C++ definitions once their containers are known: a constructor or destructor
        told apart (a method named after its class, or ~ it), and a function's or a variable's
        declaration dropped where the file defines it (same scopes, name and parameter
        types)."""
        for d in defs:
            if d.kind is not SymbolKind.METHOD:
                continue
            container = d.container
            holder = container.name if container is not None else None
            if d.owner:
                holder = d.owner[-1]
            if d.name == holder:
                d.subkind = "constructor"
            elif d.name.startswith("~"):
                d.subkind = "destructor"
        defined = {
            self._path(d)
            for d in defs
            if not d.declaration and (d.parameters is not None or d.kind is SymbolKind.VARIABLE)
        }
        return [d for d in defs if not (d.declaration and self._path(d) in defined)]

    def _path(self, d: _Def) -> tuple[str, ...]:
        """Where ``d`` is keyed (its containers' names and owners, then its name) and its
        parameter types: one function's declaration and definition share it."""
        containers: list[_Def] = []
        around = d.container
        while around is not None:  # a loop: a thousand nested structs nest a thousand deep
            containers.append(around)
            around = around.container
        outer = [part for c in reversed(containers) for part in (*c.owner, c.name)]
        return (*outer, *d.owner, d.name, d.parameters or "")

    def _owner(self, node: Node) -> tuple[str, ...]:
        """The type a definition is keyed under, as descriptors: Go's receiver type, a Rust
        impl's type, the scope of Ruby's compact class name (Api::V1 in class
        Api::V1::Orders)."""
        return tuple(p for p in self.source.node(node).split("::") if p)

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
        if not self.ecma:
            return self._declared_span(node)
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

    def _declared_span(self, node: Node) -> tuple[Node, Node]:
        """A field's annotations and modifiers are its declaration's: the field spans that,
        unless the declaration has several (private int a, b;). A Rust item's attributes stand
        before it, doc comments maybe among them. A C struct in a typedef spans the typedef; a
        C++ template its ``template <...>``."""
        span = node
        holder = self._holder(node)
        if holder is not None and len(self._declarators(holder)) == 1:
            span = holder
        elif node.type in _C_TYPES and node.parent is not None:
            span = node.parent if node.parent.type == "type_definition" else node
        while self.cpp and span.parent is not None and span.parent.type == "template_declaration":
            span = span.parent
        first = span
        previous = span.prev_named_sibling if self.rust else None
        while previous is not None and previous.type in _RUST_PREFIX:
            if previous.type == "attribute_item":
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

    def _holder(self, node: Node) -> Node | None:
        """The declaration holding a declarator (a Java, C# or PHP field, a Go spec, a C
        declaration, field or typedef), if any."""
        if self.c_family:
            parent = node.parent
            if parent is None or parent.type not in _C_HOLDERS:
                return None
            declarators = parent.children_by_field_name("declarator")
            return parent if any(d.id == node.id for d in declarators) else None
        holders = _HOLDERS.get(node.type)
        parent = node.parent
        if holders is None or parent is None:
            return None
        if parent.type in _DECLARATOR_LISTS:
            parent = parent.parent
        return parent if parent is not None and parent.type in holders else None

    def _declarators(self, holder: Node) -> list[Node]:
        """The declarators a declaration holds (a Go type declaration's specs and aliases)."""
        if self.c_family:
            return holder.children_by_field_name("declarator")
        children: list[Node] = []
        for child in holder.named_children:
            listed = child.type in _DECLARATOR_LISTS
            children.extend(child.named_children if listed else [child])
        return [c for c in children if holder.type in _HOLDERS.get(c.type, ())]

    def _container(self, d: _Def, by_node: dict[int, _Def]) -> _Def | None:
        parent = d.node.parent
        while parent is not None:
            found = by_node.get(parent.id)
            if found is not None and not self._passes_over(d, found):
                return found
            parent = parent.parent
        return None

    def _passes_over(self, d: _Def, outer: _Def) -> bool:
        """Definitions that hold ``d`` without being what it is keyed under: a C#, PHP or C++
        namespace (see the module), and the constructor a PHP property is promoted in (a
        property of the class)."""
        if (self.csharp or self.php or self.cpp) and outer.subkind == "namespace":
            return True
        return d.node.type == "property_promotion_parameter" and outer.kind is SymbolKind.METHOD

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
        package = path.stem in ("__init__", "index", "mod") and path.parent.name
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
        if self._is_async(self._callable(d.node)):
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

    @staticmethod
    def _is_async(node: Node) -> bool:
        """``async`` (Python, ECMAScript, C#, Rust) or Kotlin's ``suspend``, among the
        function's modifiers."""
        stack = list(node.children)
        while stack:
            child = stack.pop()
            if child.type in _ASYNC:
                return True
            if child.type in _MODIFIER_NODES:
                stack.extend(child.children)
        return False

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
        if not self.ecma:
            return [_collapse(self.source.node(t), 120) for t in self._base_nodes(node)]
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

    def _base_nodes(self, node: Node) -> list[Node]:
        """What a Kotlin, Go, C#, PHP, Ruby, Rust or C++ type extends and implements, in source
        order."""
        finders: dict[str, Callable[[Node], list[Node]]] = {
            "kotlin": self._kotlin_bases,
            "go": self._go_bases,
            "csharp": self._csharp_bases,
            "php": self._php_bases,
            "ruby": self._ruby_bases,
            "rust": self._rust_bases,
            "cpp": self._cpp_bases,
        }
        finder = finders.get(self.language)
        return finder(node) if finder is not None else []

    @staticmethod
    def _kotlin_bases(node: Node) -> list[Node]:
        """The delegation specifiers' types (BaseController for BaseController(), Repository
        for Repository by impl)."""
        found: list[Node] = []
        for child in node.named_children:
            if child.type != "delegation_specifiers":
                continue
            for specifier in child.named_children:
                part = specifier.named_children[0] if specifier.named_children else specifier
                if part.type in ("constructor_invocation", "explicit_delegation"):
                    part = part.named_children[0]
                found.append(part)
        return found

    @staticmethod
    def _go_bases(node: Node) -> list[Node]:
        """A struct's embedded types (*Base: Base), an interface's embedded interfaces."""
        kind = node.child_by_field_name("type") if node.type == "type_spec" else None
        members = kind.named_children if kind is not None else []
        if kind is not None and kind.type == "struct_type" and members:
            members = members[0].named_children  # its field_declaration_list
        found: list[Node] = []
        for member in members:
            embedded = member.child_by_field_name("type")
            if member.type == "type_elem":
                found.append(member)
            elif member.child_by_field_name("name") is None and embedded is not None:
                found.append(embedded)
        return found

    @staticmethod
    def _csharp_bases(node: Node) -> list[Node]:
        """The base list (a record's base without its arguments: Entity for Entity(Id))."""
        found: list[Node] = []
        for child in node.named_children:
            if child.type != "base_list":
                continue
            for base in child.named_children:
                typed = base.child_by_field_name("type")
                primary = base.type == "primary_constructor_base_type" and typed is not None
                found.append(typed if primary and typed is not None else base)
        return found

    @staticmethod
    def _php_bases(node: Node) -> list[Node]:
        """The superclass, the interfaces, then the traits the class uses."""
        found: list[Node] = []
        for child in node.named_children:
            if child.type in ("base_clause", "class_interface_clause"):
                found.extend(child.named_children)
        body = node.child_by_field_name("body")
        for use in body.named_children if body is not None else ():
            if use.type == "use_declaration":
                found.extend(n for n in use.named_children if n.type in ("name", "qualified_name"))
        return found

    def _ruby_bases(self, node: Node) -> list[Node]:
        """The superclass, then the modules the body includes or prepends."""
        if node.type not in ("class", "module"):
            return []
        superclass = node.child_by_field_name("superclass")
        found = list(superclass.named_children) if superclass is not None else []
        body = node.child_by_field_name("body")
        for call in body.named_children if body is not None else ():
            method = call.child_by_field_name("method") if call.type == "call" else None
            arguments = call.child_by_field_name("arguments")
            mixin = method is not None and self.source.node(method) in _RUBY_MIXINS
            if mixin and arguments is not None and call.child_by_field_name("receiver") is None:
                found.extend(arguments.named_children)
        return found

    def _rust_bases(self, node: Node) -> list[Node]:
        """A trait's supertraits; the traits a struct, enum or union is implemented for in its
        file (impl Display for Order)."""
        if node.type == "trait_item":
            bounds = node.child_by_field_name("bounds")
            return [b for b in bounds.named_children if b.type != "lifetime"] if bounds else []
        name = node.child_by_field_name("name")
        if node.type not in ("struct_item", "enum_item", "union_item") or name is None:
            return []
        return self._traits().get(self.source.node(name), [])

    @staticmethod
    def _cpp_bases(node: Node) -> list[Node]:
        """A class's or struct's base classes, as written (Repository<Order>), without their
        access (public) or virtual."""
        clause = next((c for c in node.named_children if c.type == "base_class_clause"), None)
        if clause is None:
            return []
        return [c for c in clause.named_children if c.type not in ("access_specifier", "virtual")]

    def _traits(self) -> dict[str, list[Node]]:
        """The traits each type of the file is implemented for (impl Display for Order), in
        source order, found once."""
        if self._implemented is None:
            self._implemented = {}
            stack = [self.root] if self.root is not None else []
            while stack:
                node = stack.pop(0)
                for child in node.named_children:
                    if child.type in ("mod_item", "declaration_list"):
                        stack.append(child)
                    if child.type != "impl_item":
                        continue
                    trait = child.child_by_field_name("trait")
                    kind = child.child_by_field_name("type")
                    name = _rust_type_name(kind) if kind is not None else None
                    if trait is not None and name is not None:
                        self._implemented.setdefault(self.source.node(name), []).append(trait)
        return self._implemented

    def _decorators(self, d: _Def) -> list[str]:
        if self.python:
            nodes = [c for c in d.span.named_children if c.type == "decorator"]
        elif self.ecma:
            nodes = self._ecma_decorators(d)
        else:
            nodes = self._annotations(d)
        wrap = {"csharp": ("[", "]"), "php": ("#[", "]")}.get(self.language, ("", ""))
        texts: list[str] = []
        for node in nodes:
            code = self.source.code(node, node.start_byte, node.end_byte)
            text = _collapse(f"{wrap[0]}{code}{wrap[1]}", MAX_DECORATOR_TEXT)
            if text not in texts:
                texts.append(text)
        return texts[:MAX_DECORATORS]

    @staticmethod
    def _ecma_decorators(d: _Def) -> list[Node]:
        """A method's decorators precede it in the class body; a class's are its own."""
        nodes: list[Node] = []
        sibling: Node | None = d.first
        while sibling is not None and sibling.type in ("decorator", "comment"):
            if sibling.type == "decorator":
                nodes.append(sibling)
            sibling = sibling.next_named_sibling
        for holder in (d.span, d.node):
            nodes.extend(c for c in holder.named_children if c.type == "decorator")
        return nodes

    def _annotations(self, d: _Def) -> list[Node]:
        """Java's and Kotlin's annotations (in the modifiers), C#'s and PHP's attributes (an
        attribute each: [A, B] and #[A, B] are two), Rust's attributes (before the item); a
        field declarator's are its declaration's."""
        holder = self._holder(d.node) or d.node
        if self.jvm:
            modifiers = next((c for c in holder.named_children if c.type == "modifiers"), None)
            children = modifiers.named_children if modifiers is not None else []
            return [c for c in children if c.type in _ANNOTATIONS]
        if self.rust:
            nodes: list[Node] = []
            sibling: Node | None = d.first
            while sibling is not None and sibling.type in _RUST_PREFIX:
                if sibling.type == "attribute_item":
                    nodes.append(sibling)
                sibling = sibling.next_named_sibling
            return nodes
        found: list[Node] = []
        stack = [c for c in holder.named_children if c.type in _ATTRIBUTE_LISTS]
        while stack:
            node = stack.pop(0)
            if node.type == "attribute":
                found.append(node)
            elif node.type in ("attribute_list", "attribute_group"):
                stack[:0] = node.named_children
        return found

    # ------------------------------------------------------------------ signatures

    def _signature(self, d: _Def) -> str | None:
        node = d.node
        if self.c_family and (text := self._c_signature(d)) is not None:
            return text
        if d.kind is SymbolKind.VARIABLE or d.subkind == "type":
            return self._value_signature(d)
        if node.type == "variable_declarator":
            target = self._callable(node)
            body = target.child_by_field_name("body")
            end = body.start_byte if body is not None else target.end_byte
            keyword = self._declaration_keyword(node)
            text = self.source.code(node, node.start_byte, end)
            return _collapse(f"{keyword} {text}".strip(), MAX_SIGNATURE)
        body = self._body(node)
        start = self._after_decorators(node)
        end = body.start_byte if body is not None else self._header_end(node)
        text = self.source.code(node, start, end).rstrip().rstrip(":;").rstrip()
        if self.ruby:
            text = text.removesuffix("=").rstrip()  # def total = lines.sum(&:amount)
        if self.go and node.type == "type_spec":
            text = f"type {text}"  # its declaration's keyword (type Server struct)
        return _collapse(text, MAX_SIGNATURE) or None

    def _c_signature(self, d: _Def) -> str | None:
        """A C or C++ declarator's declaration (one of several: its head and this declarator:
        int a = 1 and int *b for int a = 1, *b;), a lambda's up to its body; a function's or a
        type's source up to its body, its template <...> included (a typedef'd struct's body
        elided: typedef struct {...} point_t). None for the others (macros, enumerators,
        namespaces): the common rules."""
        node, span = d.node, d.span
        value = d.kind is SymbolKind.VARIABLE or d.subkind == "type"
        limit = MAX_VALUE_SIGNATURE if value else MAX_SIGNATURE
        lambda_value = node.child_by_field_name("value") if node.type == "init_declarator" else None
        lambda_body = lambda_value.child_by_field_name("body") if lambda_value is not None else None
        holder = self._holder(node)
        if holder is not None:
            declarators = self._declarators(holder)
            end = lambda_body.start_byte if lambda_body is not None else holder.end_byte
            if len(declarators) == 1:
                text = self.source.code(span, span.start_byte, end)
            else:
                head = self.source.code(holder, holder.start_byte, declarators[0].start_byte)
                own_end = lambda_body.start_byte if lambda_body is not None else node.end_byte
                text = f"{head} {self.source.code(node, node.start_byte, own_end)}"
        elif node.type in _C_BODIED:
            body = node.child_by_field_name("body")
            end = body.start_byte if body is not None else node.end_byte
            text = self.source.code(span, span.start_byte, end)
            if span.type == "type_definition" and body is not None:
                rest = self.source.code(span, body.end_byte, span.end_byte)
                text = f"{text}{{ ... }}{rest}"
        else:
            return None
        return _collapse(text.rstrip().rstrip(";").rstrip(), limit) or None

    def _body(self, node: Node) -> Node | None:
        """Where a definition's body starts: its ``body``; a Kotlin class's or function's (an
        ``= expr`` one too); a Go struct's fields, an interface's opening brace."""
        body = next(
            (b for f in _BODY_FIELDS if (b := node.child_by_field_name(f)) is not None), None
        )
        if body is not None:
            return body
        if self.kotlin:
            return next((c for c in node.children if c.type in _BODIES), None)
        if self.go and node.type == "type_spec":
            kind = node.child_by_field_name("type")
            if kind is not None and kind.type == "struct_type":
                return kind.named_children[0] if kind.named_children else None
            if kind is not None and kind.type == "interface_type":
                return next((c for c in kind.children if c.type == "{"), None)
        return None

    def _header_end(self, node: Node) -> int:
        """Where a definition without a body ends: Ruby's at its name, superclass or parameters
        (class Error < StandardError; end), a Rust macro's at its name, a C macro's at its
        parameters, any other's at its end."""
        if self.ruby or node.type in ("macro_definition", "preproc_function_def"):
            parts = [
                p
                for f in ("name", "superclass", "parameters")
                if (p := node.child_by_field_name(f))
            ]
            if parts:
                return max(p.end_byte for p in parts)
        return node.end_byte

    def _after_decorators(self, node: Node) -> int:
        for child in node.children:
            if child.type == "modifiers":  # Java, Kotlin: the annotations lead the modifiers
                kept = next(
                    (m for m in child.children if m.type not in _ANNOTATIONS | COMMENTS), None
                )
                if kept is not None:
                    return kept.start_byte
                continue
            if child.type != "decorator" and child.type not in _ATTRIBUTE_LISTS:
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
        holder = None if self.python or self.ecma else self._holder(node)
        if holder is not None:
            # The declaration's modifiers and type (Go's: its keyword), then this declarator:
            # private final OrderService orderService; private int a = 1 and private int b for
            # int a = 1, b; const MaxPage = 50.
            first = self._declarators(holder)[0]
            if self.go:
                head = self.source.node(holder.children[0])
            else:
                head = self.source.code(holder, self._after_decorators(holder), first.start_byte)
            text = f"{head} {self.source.code(node, node.start_byte, node.end_byte)}"
        elif self.jvm and node.type in ("enum_constant", "enum_entry"):
            body = self._body(node)  # up to its class body
            end = body.start_byte if body is not None else node.end_byte
            text = self.source.code(node, self._after_decorators(node), end)
        elif self.kotlin and node.type == "property_declaration":
            accessor = next((c for c in node.named_children if c.type in _ACCESSORS), None)
            end = accessor.start_byte if accessor is not None else node.end_byte
            text = self.source.code(node, self._after_decorators(node), end)
        elif node.type == "variable_declarator" and self.ecma:
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
        # One of several declarators: the declaration's doc (Go: a grouped spec's own).
        holder = None if self.python or self.ecma or self.go else self._holder(anchor)
        return self._leading_comment(holder if holder is not None else anchor)

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
        parent = node.parent
        if (
            previous is None
            and self.ruby
            and parent is not None
            and parent.type == "body_statement"
        ):
            # A body's first statement: the comment above it is the body's sibling.
            previous = parent.prev_sibling if parent.start_byte == node.start_byte else None
        while previous is not None and previous.type in COMMENTS:
            end_row = lines(previous)[1] - 1  # a Rust // comment ends on the next line
            if end_row < expected_row - 1 or not self._starts_line(previous):
                break  # a blank line between, or the end of the previous line's code
            raw = self.source.node(previous)
            if raw.startswith(("//!", "/*!")):
                break  # Rust: the docs of the module it is in
            plain = raw.startswith("/*") and not raw.startswith("/**")
            if plain and not self.ecma and not self.c_family:
                break  # a licence or code commented out, not Javadoc
            comments.insert(0, previous)
            expected_row = previous.start_point.row
            previous = previous.prev_sibling
        if not comments:
            return None
        texts: list[str] = []
        budget = MAX_DOCSTRING * 4  # what is kept is cut to MAX_DOCSTRING: read no more than this
        for comment in comments:
            for line in self.source.node(comment)[:budget].splitlines():
                stripped = line.strip()
                stripped = re.sub(r"^(/\*\*?|\*/|\*(?!/)|//+|#+)\s?", "", stripped)
                # Not a pattern: ``\s*\*/$`` backtracks on a long run of spaces.
                stripped = stripped.removesuffix("*/").rstrip()
                texts.append(stripped)
        text = "\n".join(texts)
        if self.csharp:
            text = _XML_DOC_TAGS.sub("", text)  # /// <summary>...</summary>
        text = text.strip()
        return text[:MAX_DOCSTRING] or None
