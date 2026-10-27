"""FastAPI's dependency injection as graph edges (3.5): ``INJECTS`` from a function to each
dependency FastAPI calls for it.

FastAPI declares a dependency with ``Depends(dep)`` (or ``Security(dep, ...)``):

- as a parameter's default: ``def read(db=Depends(get_db))``;
- in its annotation: ``def read(db: Annotated[Session, Depends(get_db)])``;
- through an alias of that annotation, defined once and imported: ``SessionDep =
  Annotated[Session, Depends(get_db)]``, then ``def read(session: SessionDep)`` or
  ``deps.SessionDep``;
- in a route decorator: ``@router.get("/", dependencies=[Depends(get_current_superuser)])``.

``Depends()`` with no argument depends on the annotation's type (``Annotated[Form, Depends()]``,
``q: Q = Depends()``): FastAPI calls the class.

:func:`extract` reads these from the tree in the parse pool. :func:`resolve` binds each
dependency's name at the top of its file, the way 3.4 binds names
(:class:`~sensei_engine.graph.python.calls.CallResolver`: the file's definitions, then its import
table), and an alias by what its name imports. The edge goes from the function's definition to
the dependency's (a definition of the repository, or an external's node), at the line of the
``Depends``, with the binding's provenance; it is owned by the function's file. 3.4's
``REFERENCES`` edge for the same name stays: this is the typed one.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from tree_sitter import Node

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.project import Project
from sensei_engine.graph.python.calls import CallResolver, Chain, Meaning, chain_of, type_of
from sensei_engine.graph.python.imports import Resolver, external_node
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.parse.parser import ParseResult
from sensei_engine.symbols.keys import escape

DEPENDS = frozenset({"Depends", "Security"})


@dataclass(frozen=True, slots=True)
class Injection:
    """One ``Depends`` (see the module). ``kind``: ``depends`` (in a function: its parameter or
    its decorator), ``alias`` (a module-level ``X = Annotated[..., Depends(dep)]``), ``uses``
    (a parameter annotated with a name that may be such an alias)."""

    kind: Literal["depends", "alias", "uses"]
    line: int
    target: Chain  # the dependency (depends, alias), or the annotation's name (uses)
    function: str | None = None  # the function's qualified name (depends, uses)
    function_line: int = 0  # its ``def`` line
    alias: str | None = None  # the alias's name (alias)


@dataclass(frozen=True, slots=True)
class DiFacts:
    injections: tuple[Injection, ...] = ()


@dataclass(slots=True)
class DiStats:
    dependencies: int = 0  # Depends found (and alias uses)
    injects: int = 0  # edges
    unresolved: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])


def _name(call: Node) -> str | None:
    function = call.child_by_field_name("function")
    chain = chain_of(function)
    return chain[-1] if chain else None


def _depends_calls(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "call" and _name(current) in DEPENDS:
            yield current
            continue
        stack.extend(reversed(current.children))


def _dependency(call: Node) -> Chain | None:
    """What ``Depends(dep)``/``Depends(dependency=dep)`` names (None: ``Depends()``)."""
    arguments = call.child_by_field_name("arguments")
    for argument in arguments.named_children if arguments is not None else ():
        if argument.type == "keyword_argument":
            name = argument.child_by_field_name("name")
            if name is not None and name.text == b"dependency":
                return chain_of(argument.child_by_field_name("value"))
            continue
        return chain_of(argument)  # the first positional argument
    return None


class _Walker:
    def __init__(self) -> None:
        self.found: list[Injection] = []

    def module(self, root: Node) -> None:
        for child in root.named_children:
            self._statement(child, ())

    def _statement(self, node: Node, names: tuple[str, ...]) -> None:
        if node.type == "decorated_definition":
            definition = node.child_by_field_name("definition")
            if definition is not None and definition.type == "function_definition":
                self._function(definition, names, node)
            elif definition is not None:
                self._statement(definition, names)
        elif node.type == "function_definition":
            self._function(node, names, None)
        elif node.type == "class_definition":
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name is not None and body is not None:
                for child in body.named_children:
                    self._statement(child, (*names, name.text.decode()))  # pyright: ignore[reportOptionalMemberAccess]
        elif node.type == "expression_statement" and not names:
            self._alias(node)

    def _alias(self, statement: Node) -> None:
        assignment = statement.named_children[0] if statement.named_children else None
        if assignment is None or assignment.type != "assignment":
            return
        left, right = (
            assignment.child_by_field_name("left"),
            assignment.child_by_field_name("right"),
        )
        if left is None or right is None or left.type != "identifier":
            return
        for call in _depends_calls(right):
            target = _dependency(call) or type_of(right)
            if target:
                self.found.append(
                    Injection("alias", call.start_point.row + 1, target, alias=left.text.decode())  # pyright: ignore[reportOptionalMemberAccess]
                )
            return

    def _function(self, node: Node, names: tuple[str, ...], decorated: Node | None) -> None:
        name = node.child_by_field_name("name")
        if name is None:
            return
        qualified = ".".join((*names, name.text.decode()))  # pyright: ignore[reportOptionalMemberAccess]
        line = node.start_point.row + 1

        def add(kind: Literal["depends", "uses"], at: Node, target: Chain | None) -> None:
            if target:
                self.found.append(Injection(kind, at.start_point.row + 1, target, qualified, line))

        if decorated is not None:
            for decorator in decorated.children:
                if decorator.type == "decorator":
                    for call in _depends_calls(decorator):
                        add("depends", call, _dependency(call))
        parameters = node.child_by_field_name("parameters")
        for parameter in parameters.named_children if parameters is not None else ():
            annotation = parameter.child_by_field_name("type")
            calls = list(_depends_calls(parameter))
            for call in calls:
                add("depends", call, _dependency(call) or type_of(annotation))
            if not calls and annotation is not None:
                add("uses", annotation, type_of(annotation))
        body = node.child_by_field_name("body")
        for child in body.named_children if body is not None else ():
            self._statement(child, (*names, name.text.decode()))  # pyright: ignore[reportOptionalMemberAccess]


def extract(parsed: ParseResult) -> DiFacts:
    """A Python file's ``Depends`` (see the module), from its tree in the parse pool."""
    if parsed.tree is None:
        return DiFacts()
    walker = _Walker()
    walker.module(parsed.tree.root_node)
    return DiFacts(tuple(walker.found))


class _Injections:
    def __init__(self, project: Project, calls: CallResolver, imports: Resolver) -> None:
        self.project = project
        self.calls = calls
        self.imports = imports
        self.aliases: dict[str, Meaning] = {}  # an alias's variable key: its dependency

    def function_key(self, path: str, qualified: str, line: int) -> str | None:
        for definition in self.project.definitions.get(path, ()):
            if (
                definition.qualified_name == qualified
                and definition.start_line <= line <= definition.end_line
                and definition.kind in ("function", "method")
            ):
                return definition.symbol_key
        return None

    def meaning(self, path: str, chain: Chain) -> Meaning | None:
        file = self.calls.file(path)
        if file is None:
            return None
        found = self.calls.chain(file, 0, chain)
        if found is None or found.key is None or found.kind not in ("definition", "external"):
            return None
        return found

    def alias_key(self, path: str, chain: Chain) -> str | None:
        """The variable an annotation names: ``SessionDep`` here or imported, ``deps.X``."""
        module = self.project.module_key(path)
        if len(chain) == 1:
            own = f"{module}{escape(chain[0])}." if module else None
            if own in self.aliases:
                return own
            target = self.imports.table(path).get(chain[0])
            return target.key if target is not None and not target.external else None
        if len(chain) == 2:
            target = self.imports.table(path).get(chain[0])
            if target is not None and not target.external and target.key.endswith("/"):
                return f"{target.key}{escape(chain[1])}."
        return None


def resolve(
    project: Project, facts: Mapping[str, DiFacts], calls: CallResolver, imports: Resolver
) -> tuple[list[GraphNode], list[GraphEdge], DiStats]:
    """The project's ``INJECTS`` edges and the external nodes they end at (see the module)."""
    stats = DiStats()
    injections = _Injections(project, calls, imports)
    for path in sorted(facts):
        module = project.module_key(path)
        for injection in facts[path].injections:
            if injection.kind == "alias" and module and injection.alias:
                found = injections.meaning(path, injection.target)
                if found is not None:
                    injections.aliases[f"{module}{escape(injection.alias)}."] = found
    edges: list[GraphEdge] = []
    for path in sorted(facts):
        for injection in facts[path].injections:
            if injection.kind == "alias" or injection.function is None:
                continue
            stats.dependencies += injection.kind == "depends"
            source = injections.function_key(path, injection.function, injection.function_line)
            if injection.kind == "depends":
                found = injections.meaning(path, injection.target)
            else:
                key = injections.alias_key(path, injection.target)
                found = injections.aliases.get(key) if key is not None else None
                if found is None:
                    continue  # an ordinary annotation
                stats.dependencies += 1
            if source is None or found is None or found.key is None:
                stats.unresolved.append((path, injection.line, ".".join(injection.target)))
                continue
            edges.append(
                GraphEdge(
                    src=source,
                    dst=found.key,
                    type=EdgeType.INJECTS,
                    provenance=Provenance.RECEIVER_HEURISTIC
                    if found.heuristic
                    else Provenance.IMPORT_RESOLVED,
                    evidence_path=path,
                    evidence_line=injection.line,
                    owner=path,
                )
            )
    stats.injects = len(edges)
    externals = sorted({e.dst for e in edges if e.dst.startswith(("package ", "external_symbol "))})
    return [external_node(key) for key in externals], edges, stats
