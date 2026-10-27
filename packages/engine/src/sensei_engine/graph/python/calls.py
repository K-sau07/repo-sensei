"""Python calls and references (3.4): what each call and each use of a name in a file means, with
how sure that is.

**Extraction** (:func:`extract`, in the parse pool) walks the file's tree once into its
*scopes* (the module, classes, functions; lambdas and comprehensions as unnamed ones), what each
scope binds (parameters, assignments, ``for``/``with``/``except`` targets, nested definitions,
imports in a function, ``global``), the types it can tell (annotations, ``x = Cls()``, an
``Annotated[T, ...]`` alias, a function's return annotation, ``self.x = Repo()`` and annotated
class attributes) and its *sites*: each call's callee (``f()``, ``mod.f()``, ``self.m()``,
``obj.m()``, ``Cls()``, ``Cls.static()``, ``super().m()``) and each other use of a name
(a callback passed on, a decorator, an annotation, a value read), as a dotted chain.

**Resolution** (:class:`CallResolver`) goes, for a chain's first name:

1. the local scope and the scopes around it (Python's rules: a class body is seen only from
   itself), then the file's own top-level definitions: ``import_resolved`` (0.9) for a
   definition, for what the file's import table (3.2) names, and for a module's attribute;
2. through a **receiver's type** (``self.``/``cls.`` the enclosing class, a parameter's or a
   variable's annotation, what it was assigned from, a function's return annotation, an
   attribute ``__init__`` assigned), along the class's bases: ``receiver_heuristic`` (0.7);
3. for a method called on a receiver of unknown type, the repository's methods of that name:
   ``name_unique`` (0.4) when there is one, ``name_ambiguous`` (0.2/k) to each of k ≤ 5, else
   none. A name every builtin type has (``get``, ``append``, ``items``, ...) is never guessed.

A call becomes a ``CALLS`` edge from the innermost definition around it (the module at the top
level) to what it calls, a definition or an external (``external_symbol py:sqlmodel/select``,
``external_symbol py:sqlmodel/Session.add`` for a method of an external class); a use that
isn't a call, a ``REFERENCES`` edge to the deepest definition its chain reaches (or the external
its first name imports). A class gets an ``INHERITS`` edge to each base. Every edge carries the
line it is on and is owned by its file.
"""

from __future__ import annotations

import builtins
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from tree_sitter import Node

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.python.imports import Resolver, Target, external_node
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.parse.parser import ParseResult

Chain = tuple[str, ...]  # ("crud", "create_user"); a first part UNKNOWN or SUPER: see below
UNKNOWN = "()"  # a receiver no name holds: x().m(), items[0].m()
SUPER = "super()"

MAX_CHAIN = 8
MAX_HOPS = 6  # types followed through aliases, bases and attributes
MAX_CANDIDATES = 5  # name_ambiguous fan-out; more and nothing is guessed

BUILTINS = frozenset(dir(builtins))
# Methods every builtin type has: a receiver of unknown type calling one is most likely one.
BUILTIN_METHODS = frozenset(
    name
    for kind in (str, bytes, list, dict, set, frozenset, tuple, int, float, object)
    for name in dir(kind)
)

_LITERALS = frozenset({
    "string", "concatenated_string", "integer", "float", "true", "false", "none", "list",
    "dictionary", "set", "tuple", "list_comprehension", "dictionary_comprehension",
    "set_comprehension",
})  # fmt: skip
_COMPREHENSIONS = frozenset({
    "list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression",
})  # fmt: skip
_WRAPPING_TYPES = frozenset({
    "Annotated", "Optional", "Final", "ClassVar", "Required", "NotRequired", "ReadOnly", "Type",
    "type",
})  # fmt: skip
_TARGETS = frozenset({"pattern_list", "tuple_pattern", "list_pattern", "tuple", "list"})

BindingKind = Literal["def", "class", "param", "var", "alias", "import", "global", "self", "cls"]
ScopeKind = Literal["module", "class", "function", "anonymous"]


@dataclass(frozen=True, slots=True)
class Binding:
    kind: BindingKind  # "alias": X = Annotated[T, ...] (X is T); "self"/"cls": a method's first
    type: Chain | None = None  # what its annotation names (an alias: what it stands for)
    value: Chain | None = None  # what was called to give it its value (x = Cls())
    line: int = 0


@dataclass(frozen=True, slots=True)
class Scope:
    kind: ScopeKind
    name: str  # qualified ("Item.save", "outer.inner"); "" for the module and unnamed ones
    line: int  # the def or class line
    parent: int  # the scope it is in (-1: the module)
    bindings: Mapping[str, Binding] = field(default_factory=dict[str, Binding])
    bases: tuple[Chain, ...] = ()  # a class's
    attributes: Mapping[str, Binding] = field(default_factory=dict[str, Binding])  # a class's
    returns: Chain | None = None  # a function's return annotation
    owner: int = -1  # a definition's header (decorators, annotations, defaults): the definition


@dataclass(frozen=True, slots=True)
class Site:
    line: int
    scope: int
    kind: Literal["call", "reference"]
    chain: Chain


@dataclass(frozen=True, slots=True)
class CallFacts:
    """A file's scopes and sites (see the module): picklable, read in the parse pool."""

    scopes: tuple[Scope, ...] = ()
    sites: tuple[Site, ...] = ()


# ---------------------------------------------------------------------- extraction


def _text(node: Node | None) -> str:
    raw = node.text if node is not None else None
    return (raw or b"").decode("utf-8", "replace")


def chain_of(node: Node | None) -> Chain | None:
    """``a.b.c`` as ``("a", "b", "c")``; None when it doesn't start with a name."""
    parts: list[str] = []
    while node is not None and node.type == "attribute":
        parts.append(_text(node.child_by_field_name("attribute")))
        node = node.child_by_field_name("object")
    if node is None or node.type != "identifier" or len(parts) >= MAX_CHAIN:
        return None
    parts.append(_text(node))
    return tuple(reversed(parts))


def type_of(node: Node | None) -> Chain | None:
    """The class an annotation (or an ``Annotated[...]`` value) names: ``Optional[T]``,
    ``T | None``, ``Annotated[T, ...]`` and ``"T"`` are ``T``; ``list[T]`` is ``list``."""
    for _ in range(MAX_HOPS):
        if node is None:
            return None
        if node.type in ("identifier", "attribute"):
            return chain_of(node)
        if node.type == "string":
            content = "".join(_text(c) for c in node.named_children if c.type == "string_content")
            parts = tuple(content.strip().split("."))
            return parts if all(p.isidentifier() for p in parts) else None
        if node.type in ("generic_type", "subscript"):
            base = node.named_children[0] if node.type == "generic_type" else node
            base = base.child_by_field_name("value") or base
            base_chain = chain_of(base)
            if base_chain is None or base_chain[-1] not in _WRAPPING_TYPES:
                return base_chain
            node = _first_type_argument(node)
            continue
        if node.type in ("binary_operator", "union_type"):
            options = [c for c in node.named_children if c.type != "none" and _text(c) != "None"]
            node = options[0] if options else None
            continue
        node = node.named_children[0] if node.type in ("type", "type_parameter") else None
    return None


def _first_type_argument(node: Node) -> Node | None:
    if node.type == "subscript":
        return node.child_by_field_name("subscript")
    parameters = next((c for c in node.named_children if c.type == "type_parameter"), None)
    return parameters.named_children[0] if parameters and parameters.named_children else None


def _names(target: Node | None) -> list[Node]:
    """The names an assignment target binds (``a``, ``a, (b, *c)``)."""
    if target is None:
        return []
    if target.type == "identifier":
        return [target]
    if target.type in _TARGETS or target.type in ("list_splat_pattern", "parenthesized_expression"):
        return [name for child in target.named_children for name in _names(child)]
    return []


@dataclass(slots=True)
class _Scope:
    kind: ScopeKind
    name: str
    line: int
    parent: int
    bindings: dict[str, Binding] = field(default_factory=dict[str, Binding])
    bases: list[Chain] = field(default_factory=list[Chain])
    attributes: dict[str, Binding] = field(default_factory=dict[str, Binding])
    returns: Chain | None = None
    owner: int = -1

    def bind(self, name: str, binding: Binding) -> None:
        held = self.bindings.get(name)
        if held is None or (held.kind in ("var", "param") and binding.kind != "var"):
            self.bindings[name] = binding
        elif held.kind == "var" and held.type is None and held.value is None:
            self.bindings[name] = binding  # a later assignment tells its type

    def freeze(self) -> Scope:
        return Scope(
            self.kind,
            self.name,
            self.line,
            self.parent,
            self.bindings,
            tuple(self.bases),
            self.attributes,
            self.returns,
            self.owner,
        )


Stack = list[tuple[Node, int]]


class _Walker:
    def __init__(self) -> None:
        self.scopes: list[_Scope] = [_Scope("module", "", 0, -1)]
        self.sites: list[Site] = []
        self._handlers: dict[str, Callable[[Node, int, Stack], None]] = {
            "identifier": self._identifier,
            "attribute": self._attribute,
            "call": self._call,
            "keyword_argument": self._keyword_argument,
            "decorated_definition": self._decorated,
            "function_definition": self._function,
            "class_definition": self._class,
            "lambda": self._lambda,
            "assignment": self._assignment,
            "augmented_assignment": self._augmented,
            "for_statement": self._for,
            "for_in_clause": self._for,
            "as_pattern": self._as_pattern,
            "named_expression": self._named_expression,
            "global_statement": self._global,
            "nonlocal_statement": self._skip,
            "import_statement": self._import,
            "import_from_statement": self._import,
            "future_import_statement": self._skip,
            "string": self._string,
            **dict.fromkeys(_COMPREHENSIONS, self._comprehension),
        }

    def run(self, root: Node) -> CallFacts:
        stack: Stack = [(root, 0)]
        while stack:
            node, scope = stack.pop()
            handler = self._handlers.get(node.type)
            if handler is not None:
                handler(node, scope, stack)
            else:
                stack.extend((child, scope) for child in node.named_children)
        return CallFacts(
            tuple(s.freeze() for s in self.scopes), tuple(_kept(self.scopes, self.sites))
        )

    def _site(
        self, node: Node, scope: int, kind: Literal["call", "reference"], chain: Chain
    ) -> None:
        self.sites.append(Site(node.start_point.row + 1, scope, kind, chain))

    def _open(self, kind: ScopeKind, name: str, node: Node, parent: int) -> int:
        outer = self._named(parent)
        qualified = f"{outer}.{name}" if outer and name else name
        self.scopes.append(_Scope(kind, qualified, node.start_point.row + 1, parent))
        return len(self.scopes) - 1

    def _header(self, node: Node, outer: int, inner: int, stack: Stack) -> int:
        """A definition's header: names resolve around the definition, uses are the
        definition's (``@router.get``, ``session: SessionDep`` are ``read_items``')."""
        header = self._open("anonymous", "", node, outer)
        self.scopes[header].owner = inner
        decorated = (
            node.parent if node.parent and node.parent.type == "decorated_definition" else None
        )
        for child in decorated.named_children if decorated else ():
            if child.type == "decorator":
                stack.extend((expression, header) for expression in child.named_children)
        return header

    def _named(self, scope: int) -> str:
        while scope >= 0 and self.scopes[scope].kind == "anonymous":
            scope = self.scopes[scope].parent
        return self.scopes[scope].name if scope >= 0 else ""

    # ------------------------------------------------------------------ expressions

    def _identifier(self, node: Node, scope: int, stack: Stack) -> None:
        self._site(node, scope, "reference", (_text(node),))

    def _attribute(self, node: Node, scope: int, stack: Stack) -> None:
        chain = chain_of(node)
        if chain is not None:
            self._site(node, scope, "reference", chain)
        else:
            stack.append((node.child_by_field_name("object") or node, scope))

    def _call(self, node: Node, scope: int, stack: Stack) -> None:
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if arguments is not None:
            stack.append((arguments, scope))
        if function is None:
            return
        chain = chain_of(function)
        if chain is not None:
            self._site(function, scope, "call", chain)
            return
        receiver = function.child_by_field_name("object") if function.type == "attribute" else None
        if receiver is None:
            stack.append((function, scope))
            return
        method = _text(function.child_by_field_name("attribute"))
        if receiver.type == "call" and _text(receiver.child_by_field_name("function")) == "super":
            self._site(function, scope, "call", (SUPER, method))
        elif receiver.type not in _LITERALS:
            self._site(function, scope, "call", (UNKNOWN, method))
        stack.append((receiver, scope))

    def _keyword_argument(self, node: Node, scope: int, stack: Stack) -> None:
        value = node.child_by_field_name("value")
        if value is not None:
            stack.append((value, scope))

    def _decorated(self, node: Node, scope: int, stack: Stack) -> None:
        definition = node.child_by_field_name("definition")  # its decorators: its header's
        if definition is not None:
            stack.append((definition, scope))

    def _string(self, node: Node, scope: int, stack: Stack) -> None:
        stack.extend((c, scope) for c in node.named_children if c.type == "interpolation")

    def _skip(self, node: Node, scope: int, stack: Stack) -> None:
        return

    # ------------------------------------------------------------------ definitions

    def _function(self, node: Node, scope: int, stack: Stack) -> None:
        name = _text(node.child_by_field_name("name"))
        line = node.start_point.row + 1
        self.scopes[scope].bind(name, Binding("def", line=line))
        inner = self._open("function", name, node, scope)
        header = self._header(node, scope, inner, stack)
        returns = node.child_by_field_name("return_type")
        if returns is not None:
            self.scopes[inner].returns = type_of(returns)
            stack.append((returns, header))
        first = self._first_kind(node, scope)
        parameters = node.child_by_field_name("parameters")
        for index, parameter in enumerate(parameters.named_children if parameters else ()):
            self._parameter(parameter, header, inner, stack, first if index == 0 else None)
        body = node.child_by_field_name("body")
        if body is not None:
            stack.append((body, inner))

    def _first_kind(self, node: Node, scope: int) -> BindingKind | None:
        """What a method's first parameter is: the instance, the class, or nothing special."""
        if self.scopes[scope].kind != "class":
            return None
        decorated = (
            node.parent if node.parent and node.parent.type == "decorated_definition" else None
        )
        names = {
            _text(d.named_children[0]) for d in (decorated.named_children if decorated else ())
            if d.type == "decorator" and d.named_children
        }  # fmt: skip
        if "staticmethod" in names:
            return None
        return "cls" if "classmethod" in names or _text(node.child_by_field_name("name")) in (
            "__new__", "__init_subclass__", "__class_getitem__",
        ) else "self"  # fmt: skip

    def _parameter(
        self, node: Node, outer: int, inner: int, stack: Stack, first: BindingKind | None
    ) -> None:
        name_node: Node | None = node
        annotation = node.child_by_field_name("type")
        if node.type in ("default_parameter", "typed_default_parameter"):
            name_node = node.child_by_field_name("name")
            default = node.child_by_field_name("value")
            if default is not None:
                stack.append((default, outer))
        elif node.type == "typed_parameter":
            name_node = next((c for c in node.named_children if c.type != "type"), None)
        if name_node is not None and name_node.type in (
            "list_splat_pattern", "dictionary_splat_pattern",
        ):  # fmt: skip
            name_node, first = (name_node.named_children or [None])[0], None
        if annotation is not None:
            stack.append((annotation, outer))
        if name_node is None or name_node.type != "identifier":
            return
        line = node.start_point.row + 1
        if first is not None and annotation is None:
            binding = Binding(first, line=line)
        else:
            binding = Binding("param", type_of(annotation), line=line)
        self.scopes[inner].bind(_text(name_node), binding)

    def _class(self, node: Node, scope: int, stack: Stack) -> None:
        name = _text(node.child_by_field_name("name"))
        self.scopes[scope].bind(name, Binding("class", line=node.start_point.row + 1))
        inner = self._open("class", name, node, scope)
        header = self._header(node, scope, inner, stack)
        superclasses = node.child_by_field_name("superclasses")
        for base in superclasses.named_children if superclasses else ():
            chain = chain_of(base) if base.type != "keyword_argument" else None
            if chain is not None:
                self.scopes[inner].bases.append(chain)  # INHERITS edges
            else:
                stack.append((base, header))
        body = node.child_by_field_name("body")
        if body is not None:
            stack.append((body, inner))

    def _lambda(self, node: Node, scope: int, stack: Stack) -> None:
        inner = self._open("anonymous", "", node, scope)
        parameters = node.child_by_field_name("parameters")
        for parameter in parameters.named_children if parameters else ():
            self._parameter(parameter, scope, inner, stack, None)
        body = node.child_by_field_name("body")
        if body is not None:
            stack.append((body, inner))

    def _comprehension(self, node: Node, scope: int, stack: Stack) -> None:
        inner = self._open("anonymous", "", node, scope)
        stack.extend((child, inner) for child in node.named_children)

    # ------------------------------------------------------------------ bindings

    def _assignment(self, node: Node, scope: int, stack: Stack) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        annotation = node.child_by_field_name("type")
        line = node.start_point.row + 1
        if annotation is not None:
            stack.append((annotation, scope))
        if right is not None:
            stack.append((right, scope))
        while right is not None and right.type == "assignment":  # a = b = Cls()
            right = right.child_by_field_name("right")
        binding = self._binding_of(annotation, right, line)
        if left is not None and left.type == "attribute":
            self._attribute_assigned(left, scope, binding, stack)
            return
        if left is not None and left.type == "subscript":
            stack.append((left, scope))
        names = _names(left)
        for name in names:
            self.scopes[scope].bind(_text(name), binding if len(names) == 1 else Binding("var"))
        if len(names) == 1 and self.scopes[scope].kind == "class":
            self.scopes[scope].attributes[_text(names[0])] = binding

    @staticmethod
    def _binding_of(annotation: Node | None, right: Node | None, line: int) -> Binding:
        if annotation is not None:
            return Binding("var", type_of(annotation), line=line)
        if right is not None and right.type == "call":
            return Binding("var", value=chain_of(right.child_by_field_name("function")), line=line)
        if right is not None and right.type == "subscript":
            base = chain_of(right.child_by_field_name("value"))
            if base is not None and base[-1] in _WRAPPING_TYPES:
                return Binding("alias", type_of(right), line=line)
        return Binding("var", line=line)

    def _attribute_assigned(self, left: Node, scope: int, binding: Binding, stack: Stack) -> None:
        """``self.x = ...`` in a method: an attribute of the class's instances."""
        receiver = left.child_by_field_name("object")
        held = self.scopes[scope].bindings.get(_text(receiver)) if receiver else None
        parent = self.scopes[scope].parent
        if (
            receiver is not None
            and receiver.type == "identifier"
            and held is not None
            and held.kind == "self"
            and parent >= 0
            and self.scopes[parent].kind == "class"
        ):
            attributes = self.scopes[parent].attributes
            name = _text(left.child_by_field_name("attribute"))
            if name not in attributes or attributes[name].type is attributes[name].value is None:
                attributes[name] = binding
        elif receiver is not None:
            stack.append((receiver, scope))

    def _augmented(self, node: Node, scope: int, stack: Stack) -> None:
        left = node.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            self.scopes[scope].bind(_text(left), Binding("var"))
        elif left is not None:
            stack.append((left, scope))
        right = node.child_by_field_name("right")
        if right is not None:
            stack.append((right, scope))

    def _for(self, node: Node, scope: int, stack: Stack) -> None:
        left = node.child_by_field_name("left")
        for name in _names(left):
            self.scopes[scope].bind(_text(name), Binding("var"))
        stack.extend((c, scope) for c in node.named_children if c != left)

    def _as_pattern(self, node: Node, scope: int, stack: Stack) -> None:
        value = node.named_children[0] if node.named_children else None
        alias = node.child_by_field_name("alias")
        if value is not None:
            stack.append((value, scope))
        if alias is None:
            return
        names = _names(alias.named_children[0] if alias.named_children else None)
        if len(names) != 1:
            for name in names:
                self.scopes[scope].bind(_text(name), Binding("var"))
            return
        line = node.start_point.row + 1
        in_except = node.parent is not None and node.parent.type == "except_clause"
        if in_except:  # except ValueError as error: an instance of ValueError
            binding = Binding("var", type_of(value), line=line)
        elif value is not None and value.type == "call":  # with Session(engine) as session
            binding = Binding(
                "var", value=chain_of(value.child_by_field_name("function")), line=line
            )
        else:
            binding = Binding("var", line=line)
        self.scopes[scope].bind(_text(names[0]), binding)

    def _named_expression(self, node: Node, scope: int, stack: Stack) -> None:
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is not None:
            self.scopes[scope].bind(_text(name), self._binding_of(None, value, 0))
        if value is not None:
            stack.append((value, scope))

    def _global(self, node: Node, scope: int, stack: Stack) -> None:
        for name in node.named_children:
            self.scopes[scope].bindings[_text(name)] = Binding("global")

    def _import(self, node: Node, scope: int, stack: Stack) -> None:
        """An import in a function binds its names there (what they mean: the file's table)."""
        if scope == 0:
            return
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                local = _text(child.child_by_field_name("alias"))
            elif node.type == "import_statement":
                local = _text(child).split(".", maxsplit=1)[0]
            else:
                local = _text(child)
            self.scopes[scope].bind(local, Binding("import", line=node.start_point.row + 1))


def _kept(scopes: list[_Scope], sites: Iterable[Site]) -> Iterable[Site]:
    """The sites that can mean a definition or an external: not a use of a local value, nor a
    builtin's (``len(x)``) when nothing in the file binds the name."""
    module = scopes[0].bindings
    for site in sorted(sites, key=lambda s: (s.line, s.kind, s.chain)):
        root = site.chain[0]
        held = _held(scopes, site.scope, root)
        if (
            held is not None
            and held.kind in ("param", "var", "self", "cls")
            and len(site.chain) == 1
            and (site.kind == "reference" or held.kind == "param")
        ):
            continue  # a local value: no definition (a local function is bound "def")
        if held is None and root not in module and root in BUILTINS and len(site.chain) == 1:
            continue
        yield site


def _held(scopes: list[_Scope], scope: int, name: str) -> Binding | None:
    first = True
    while scope > 0:
        here = scopes[scope]
        if (first or here.kind != "class") and name in here.bindings:
            held = here.bindings[name]
            return None if held.kind == "global" else held
        first, scope = first and here.kind == "anonymous", here.parent
    return None


def extract(parsed: ParseResult) -> CallFacts:
    """A file's scopes and call and reference sites, from its parse (in the pool)."""
    if not parsed.ok or parsed.tree is None:
        return CallFacts()
    return _Walker().run(parsed.tree.root_node)


# ---------------------------------------------------------------------- resolution

MeaningKind = Literal[
    "definition", "module", "external", "instance", "external_instance", "dead_end", "unknown",
]  # fmt: skip


@dataclass(frozen=True, slots=True)
class Meaning:
    """What a name or a chain stands for. ``dead_end``: known, but not one the graph has (a
    builtin, a module's missing name, an external's result); ``unknown``: a value of unknown
    type (a method on it may be guessed by name)."""

    kind: MeaningKind
    key: str | None = None  # a node key (an instance's: its class's)
    path: str | None = None  # the file the definition or module is in
    heuristic: bool = False  # reached through a value's type (receiver_heuristic)


DEAD = Meaning("dead_end")
UNKNOWN_VALUE = Meaning("unknown")


@dataclass(slots=True)
class CallStats:
    calls: int = 0  # call sites
    resolved: int = 0  # ... with an edge to a definition or an external
    guessed: int = 0  # ... by name only (name_unique, name_ambiguous)
    unresolved: int = 0  # ... to nothing the graph has
    by_provenance: Counter[str] = field(default_factory=Counter[str])  # edges


@dataclass(frozen=True, slots=True)
class PyFile:
    path: str
    facts: CallFacts
    definitions: Mapping[str, tuple[Definition, ...]]  # by qualified name
    module_key: str | None


class CallResolver:
    """Resolves a project's Python calls and references (see the module), with the import
    tables of ``imports``."""

    def __init__(self, project: Project, facts: Mapping[str, CallFacts], imports: Resolver) -> None:
        self._project = project
        self._imports = imports
        self._files: dict[str, PyFile] = {}
        for path, file_facts in facts.items():
            by_name: dict[str, list[Definition]] = {}
            for definition in project.definitions.get(path, ()):
                by_name.setdefault(definition.qualified_name, []).append(definition)
            self._files[path] = PyFile(
                path,
                file_facts,
                {name: tuple(found) for name, found in by_name.items()},
                project.module_key(path),
            )
        self._kinds = {
            d.symbol_key: (d.kind, path)
            for path, found in project.definitions.items()
            for d in found
        }
        self._methods: dict[str, list[str]] = {}
        for path, found in project.definitions.items():
            if not path.endswith((".py", ".pyi")):
                continue
            for d in found:
                if d.kind == "method":
                    self._methods.setdefault(d.name, []).append(d.symbol_key)
        self._class_scopes: dict[str, tuple[PyFile, int]] = {}
        for file in self._files.values():
            for index, scope in enumerate(file.facts.scopes):
                if scope.kind == "class":
                    key = self._key(file, scope.name, scope.line)
                    if key is not None:
                        self._class_scopes.setdefault(key, (file, index))
        self.stats = CallStats()

    def file(self, path: str) -> PyFile | None:
        """A file's facts, for :meth:`chain` (None: not a Python file of the project)."""
        return self._files.get(path)

    # ------------------------------------------------------------------ definitions

    def _key(self, file: PyFile, qualified: str, line: int) -> str | None:
        found = file.definitions.get(qualified, ())
        inside = [d for d in found if d.start_line <= line <= d.end_line]
        chosen = (inside or list(found) or [None])[0]
        return chosen.symbol_key if chosen is not None else None

    def _source(self, file: PyFile, scope: int) -> str | None:
        """The key of the innermost definition around a scope (the module's at the top)."""
        scopes = file.facts.scopes
        while scope > 0:
            here = scopes[scope]
            if here.owner > 0:
                scope = here.owner
                continue
            if here.kind != "anonymous":
                key = self._key(file, here.name, here.line)
                if key is not None:
                    return key
            scope = here.parent
        return file.module_key

    def _definition(self, key: str, heuristic: bool = False) -> Meaning:
        kind, path = self._kinds.get(key, ("", None))
        if kind == "module":
            return Meaning("module", key, path, heuristic)
        return Meaning("definition", key, path, heuristic)

    def _target(self, target: Target, heuristic: bool = False) -> Meaning:
        if target.external:
            return Meaning("external", target.key, None, heuristic)
        return self._definition(target.key, heuristic)

    # ------------------------------------------------------------------ names

    def lookup(self, file: PyFile, scope: int, name: str, hops: int = 0) -> Meaning | None:
        """What ``name`` means in a scope of ``file`` (None: nothing binds it)."""
        scopes = file.facts.scopes
        first = True
        while scope > 0:
            here = scopes[scope]
            held = here.bindings.get(name) if first or here.kind != "class" else None
            if held is not None:
                if held.kind == "global":
                    break
                return self._binding(file, scope, name, held, hops)
            first, scope = first and here.kind == "anonymous", here.parent
        return self._module_name(file, name, hops)

    def _module_name(self, file: PyFile, name: str, hops: int) -> Meaning | None:
        held = file.facts.scopes[0].bindings.get(name) if file.facts.scopes else None
        if held is not None and held.kind != "import":
            key = self._key(file, name, held.line)
            if key is not None:
                return self._definition(key)
            return self._value(file, 0, held, hops)  # bound, not a definition: in a loop, ...
        target = self._imports.table(file.path).get(name)
        if target is not None:
            return self._target(target)
        if name in BUILTINS:
            return DEAD
        return None

    def _binding(self, file: PyFile, scope: int, name: str, held: Binding, hops: int) -> Meaning:
        here = file.facts.scopes[scope]
        if held.kind in ("def", "class"):
            qualified = f"{here.name}.{name}" if here.name else name
            key = self._key(file, qualified, held.line)
            return self._definition(key) if key is not None else DEAD
        if held.kind == "import":
            target = self._imports.table(file.path).get(name)
            return self._target(target) if target is not None else DEAD
        if held.kind in ("self", "cls"):
            owner = file.facts.scopes[here.parent] if here.parent >= 0 else None
            key = self._key(file, owner.name, owner.line) if owner is not None else None
            if key is None:
                return UNKNOWN_VALUE
            return Meaning(
                "instance" if held.kind == "self" else "definition", key, file.path, True
            )
        return self._value(file, scope, held, hops)

    def _value(self, file: PyFile, scope: int, held: Binding, hops: int) -> Meaning:
        """A value's meaning from what its binding tells of its type (an alias: the type)."""
        if hops >= MAX_HOPS:
            return UNKNOWN_VALUE
        if held.type is not None:
            typed = self.chain(file, scope, held.type, hops + 1)
            if held.kind == "alias":
                return typed or UNKNOWN_VALUE
            return self._instance(typed, hops)
        if held.value is not None:
            return self._called(self.chain(file, scope, held.value, hops + 1), hops)
        return UNKNOWN_VALUE

    def _instance(self, meaning: Meaning | None, hops: int) -> Meaning:
        """An instance of what a type names."""
        if meaning is None or meaning.kind == "unknown":
            return UNKNOWN_VALUE
        if meaning.kind == "external" and meaning.key:
            return Meaning("external_instance", meaning.key, None, True)
        kind, path = self._kinds.get(meaning.key or "", ("", None))
        if meaning.kind != "definition" or kind not in ("class", "variable"):
            return DEAD
        if kind == "variable":  # an alias: SessionDep = Annotated[Session, ...]
            return self._instance(self._variable(meaning, hops), hops + 1)
        return Meaning("instance", meaning.key, path, True)

    def _called(self, callee: Meaning | None, hops: int) -> Meaning:
        """What calling something gives: a class's instance, a function's return type (calling
        an external gives what the graph doesn't follow)."""
        if callee is None or callee.kind == "unknown":
            return UNKNOWN_VALUE
        kind, _ = self._kinds.get(callee.key or "", ("", None))
        if callee.kind == "external" and callee.key and _class_like(callee.key):
            return Meaning("external_instance", callee.key, None, True)  # router = APIRouter()
        if callee.kind != "definition" or not callee.key:
            return DEAD
        if kind == "class":
            return Meaning("instance", callee.key, callee.path, True)
        located = self._scope_of(callee.key) if kind in ("function", "method") else None
        returns = located[0].facts.scopes[located[1]].returns if located else None
        if located is None or returns is None:
            return UNKNOWN_VALUE
        file, index = located
        parent = max(file.facts.scopes[index].parent, 0)
        return self._instance(self.chain(file, parent, returns, hops + 1), hops)

    def _scope_of(self, key: str) -> tuple[PyFile, int] | None:
        _, path = self._kinds.get(key, ("", None))
        file = self._files.get(path) if path is not None else None
        if file is None:
            return None
        for index, scope in enumerate(file.facts.scopes):
            if (
                scope.kind in ("function", "class")
                and self._key(file, scope.name, scope.line) == key
            ):
                return file, index
        return None

    def _variable(self, meaning: Meaning, hops: int) -> Meaning:
        """The value of a variable definition (a module's, or a class attribute)."""
        _, path = self._kinds.get(meaning.key or "", ("", None))
        file = self._files.get(path) if path is not None else None
        if file is None or not file.facts.scopes:
            return UNKNOWN_VALUE
        definition = next(
            (
                d
                for found in file.definitions.values()
                for d in found
                if d.symbol_key == meaning.key
            ),
            None,
        )
        if definition is None:
            return UNKNOWN_VALUE
        owner, _, name = definition.qualified_name.rpartition(".")
        if not owner:
            held = file.facts.scopes[0].bindings.get(name)
            return self._value(file, 0, held, hops) if held is not None else UNKNOWN_VALUE
        located = next(
            (
                i
                for i, s in enumerate(file.facts.scopes)
                if s.kind == "class" and s.name == owner
                and s.line <= definition.end_line
            ),
            None,
        )  # fmt: skip
        if located is None:
            return UNKNOWN_VALUE
        held = file.facts.scopes[located].attributes.get(name)
        return self._value(file, located, held, hops) if held is not None else UNKNOWN_VALUE

    # ------------------------------------------------------------------ chains

    def chain(self, file: PyFile, scope: int, chain: Chain, hops: int = 0) -> Meaning | None:
        """What a whole chain means (None: its first name is bound by nothing)."""
        meanings = self.walk(file, scope, chain, hops)
        return meanings[-1] if meanings else None

    def walk(self, file: PyFile, scope: int, chain: Chain, hops: int = 0) -> list[Meaning]:
        """The meaning of each prefix of the chain, up to the first that leads nowhere."""
        root = chain[0]
        if root == UNKNOWN:
            first: Meaning | None = UNKNOWN_VALUE
        elif root == SUPER:
            first = self._super(file, scope)
        else:
            first = self.lookup(file, scope, root, hops)
        if first is None:
            return []
        meanings = [first]
        for name in chain[1:]:
            here = meanings[-1]
            if here.kind in ("dead_end", "unknown"):
                meanings.append(here if here.kind == "dead_end" else UNKNOWN_VALUE)
                continue
            meanings.append(self.attribute(here, name, hops))
        return meanings

    def _super(self, file: PyFile, scope: int) -> Meaning:
        """``super()`` in a method: the instance, seen from the class's bases."""
        scopes = file.facts.scopes
        while scope > 0 and scopes[scope].kind != "function":
            scope = scopes[scope].parent
        owner = scopes[scopes[scope].parent] if scope > 0 and scopes[scope].parent > 0 else None
        if owner is None or owner.kind != "class":
            return DEAD
        key = self._key(file, owner.name, owner.line)
        return Meaning("instance", f"super {key}", file.path, True) if key else DEAD

    def attribute(self, meaning: Meaning, name: str, hops: int) -> Meaning:
        """What ``<meaning>.<name>`` means."""
        key = meaning.key
        if key is None or hops >= MAX_HOPS:
            return DEAD
        if meaning.kind in ("external", "external_instance"):
            return Meaning("external", _external_attribute(key, name), None, meaning.heuristic)
        if meaning.kind == "module":
            return self._module_attribute(meaning, name)
        if meaning.kind == "instance":
            skip_own = key.startswith("super ")
            return self._member(
                key.removeprefix("super "), name, hops, heuristic=True, skip_own=skip_own
            )
        return self._definition_attribute(meaning, key, name, hops)

    def _definition_attribute(self, meaning: Meaning, key: str, name: str, hops: int) -> Meaning:
        kind, _ = self._kinds.get(key, ("", None))
        if kind == "class":  # Cls.attr: static, as sure as the class
            return self._member(key, name, hops, heuristic=meaning.heuristic)
        if kind == "variable":
            return self.attribute(self._variable(meaning, hops + 1), name, hops + 1)
        return DEAD

    def _module_attribute(self, module: Meaning, name: str) -> Meaning:
        dotted = self._imports.module_name(module.path or "")
        found = self._imports.name_in(dotted, name) if dotted is not None else None
        return self._target(found, module.heuristic) if found is not None else DEAD

    def _member(
        self, class_key: str, name: str, hops: int, *, heuristic: bool, skip_own: bool = False
    ) -> Meaning:
        """A class's (or its bases') attribute, by the class's and bases' definitions, then
        the attributes its methods assign."""
        seen: set[str] = set()
        queue = [(class_key, skip_own)]
        external_base: str | None = None
        while queue and len(seen) <= MAX_HOPS:
            current, skip = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            located = self._class_scopes.get(current)
            if located is None:
                continue
            file, index = located
            scope = file.facts.scopes[index]
            if not skip:
                key = self._key(file, f"{scope.name}.{name}", scope.line)
                if key is not None:
                    return self._definition(key, heuristic)
                held = scope.attributes.get(name)
                if held is not None and (held.type or held.value):
                    return self._value(file, index, held, hops + 1)
            for base in scope.bases:
                found = self.chain(file, max(scope.parent, 0), base, hops + 1)
                if found is not None and found.kind == "definition" and found.key:
                    queue.append((found.key, False))
                elif found is not None and found.kind == "external" and found.key:
                    external_base = external_base or found.key
        if external_base is not None:  # from an external base: model_dump(), ...
            return Meaning("external", _external_attribute(external_base, name), None, True)
        return DEAD

    # ------------------------------------------------------------------ edges

    def edges(self, path: str) -> list[GraphEdge]:
        file = self._files.get(path)
        if file is None or file.module_key is None:
            return []
        add = _Edges(path)
        for site in file.facts.sites:
            source = self._source(file, site.scope)
            if source is None:
                continue
            if site.kind == "call":
                self._call(file, site, source, add)
            else:
                self._reference(file, site, source, add)
        for scope in file.facts.scopes:
            key = self._key(file, scope.name, scope.line) if scope.kind == "class" else None
            if key is None:
                continue
            for base in scope.bases:
                meaning = self.chain(file, max(scope.parent, 0), base)
                if meaning is not None and meaning.kind in ("definition", "external"):
                    add(key, meaning.key or "", EdgeType.INHERITS, scope.line, _provenance(meaning))
        return list(add.found.values())

    def _call(self, file: PyFile, site: Site, source: str, add: _Edges) -> None:
        self.stats.calls += 1
        meanings = self.walk(file, site.scope, site.chain)
        final = meanings[-1] if meanings else None
        if final is not None and final.kind in ("definition", "external") and final.key:
            self.stats.resolved += 1
            add(source, final.key, EdgeType.CALLS, site.line, _provenance(final))
            return
        receiver = meanings[-2] if len(meanings) >= 2 else None
        name = site.chain[-1]
        if (
            receiver is not None
            and receiver.kind == "unknown"
            and name not in BUILTIN_METHODS
            and not name.startswith("__")
        ):
            candidates = self._methods.get(name, [])
            if 1 <= len(candidates) <= MAX_CANDIDATES:
                self.stats.resolved += 1
                self.stats.guessed += 1
                how = Provenance.NAME_UNIQUE if len(candidates) == 1 else Provenance.NAME_AMBIGUOUS
                for candidate in candidates:
                    add(source, candidate, EdgeType.CALLS, site.line, how, k=len(candidates))
                return
        self.stats.unresolved += 1

    def _reference(self, file: PyFile, site: Site, source: str, add: _Edges) -> None:
        meanings = self.walk(file, site.scope, site.chain)
        # The deepest definition the chain reaches (settings.API_V1_STR: the field; an unknown
        # field of it: settings); an external only as the name the file imports.
        reached = [
            m
            for i, m in enumerate(meanings)
            if (m.kind == "definition" and m.key) or (m.kind == "external" and i == 0 and m.key)
        ]
        if not reached or reached[-1].key == source:
            return
        add(source, reached[-1].key or "", EdgeType.REFERENCES, site.line, _provenance(reached[-1]))


class _Edges:
    """A file's edges, one per (ends, type, line): the more certain stays."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.found: dict[tuple[str, str, EdgeType, int], GraphEdge] = {}

    def __call__(
        self, source: str, target: str, kind: EdgeType, line: int, how: Provenance, *, k: int = 1
    ) -> None:
        if not target:
            return
        edge = GraphEdge(
            source,
            target,
            kind,
            how,
            confidence=confidence_of(how, k),
            evidence_path=self.path,
            evidence_line=line,
            owner=self.path,
        )
        held = self.found.get((source, target, kind, line))
        if held is None or (held.confidence or 0) < (edge.confidence or 0):
            self.found[(source, target, kind, line)] = edge


def _class_like(key: str) -> bool:
    """Whether an external's name is a class's, by Python's convention (``APIRouter``)."""
    name = key.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return name[:1].isupper() and not name.isupper()


def _provenance(meaning: Meaning) -> Provenance:
    return Provenance.RECEIVER_HEURISTIC if meaning.heuristic else Provenance.IMPORT_RESOLVED


def _external_attribute(key: str, name: str) -> str:
    """An external's attribute: ``package py:os`` + path is ``external_symbol py:os/path``,
    ``external_symbol py:sqlmodel/Session`` + add is ``external_symbol py:sqlmodel/Session.add``."""
    if key.startswith("package "):
        return f"external_symbol {key.removeprefix('package ')}/{name}"
    return f"{key}.{name}"


def external_node_keys(edges: Iterable[GraphEdge]) -> set[str]:
    """The externals the edges end at (each needs its node)."""
    return {
        end
        for edge in edges
        for end in (edge.dst,)
        if end.startswith(("package ", "external_symbol "))
    }


def resolve_calls(
    project: Project, facts: Mapping[str, CallFacts], imports: Resolver
) -> tuple[list[GraphNode], list[GraphEdge], CallStats]:
    """The project's Python call graph: ``CALLS``, ``REFERENCES`` and ``INHERITS`` edges (each
    owned by its file), and the external nodes they end at."""
    resolver = CallResolver(project, facts, imports)
    edges: list[GraphEdge] = []
    for path in sorted(facts):
        edges.extend(resolver.edges(path))
    for edge in edges:
        resolver.stats.by_provenance[edge.provenance.value] += 1
    nodes = [external_node(key) for key in sorted(external_node_keys(edges))]
    return nodes, edges, resolver.stats
