"""ORM models, tables and the code that reads and writes them (3.11), for Python: SQLAlchemy and
SQLModel.

Two passes. :func:`scan` reads one parsed file (no other file needed) and finds:

- **classes** and their fields: a model is a class with ``table=True`` (SQLModel: its table is
  ``__tablename__``, else the class name lower-cased), a string ``__tablename__`` (declarative
  SQLAlchemy) or a ``__table__ = Table("name", ...)``. Its columns are its fields (SQLModel: the
  annotated ones; SQLAlchemy: ``Column``/``mapped_column`` and bare ``Mapped[...]``), the fields
  of the non-table classes it inherits included; relationships and ``ClassVar``s aren't columns;
- ``Table("name", metadata, Column("c"), ...)`` objects;
- **query sites**, in the function they are in: ``select(M)``, ``.select_from(M)``,
  ``.join(M)``, ``session.query(M)`` and ``session.get(M, …)`` read; ``insert/update/delete(M)``
  write; ``session.add/add_all/delete/merge(x)`` write the model ``x`` is, when the function
  says: ``x = M(...)``, ``M.model_validate(...)``, ``session.get(M, …)``, a call to a function
  annotated to return ``M``, or a parameter annotated ``M`` (through ``Optional``,
  ``Annotated`` and module-level aliases such as ``CurrentUser = Annotated[User, ...]``);
- **raw SQL**: a string literal passed to ``text(...)``, ``.execute(...)`` or
  ``.exec_driver_sql(...)`` that reads as a statement (:mod:`sensei_engine.link.sql`);
- **migrations**: Alembic's ``op.create_table/drop_table/rename_table/add_column``.

:func:`link` then joins the files: a name is resolved through the file's imports (a model in
the module it's imported from, or in the same file: ``import_resolved``), else by name among all
the models (``name_unique``, or ``name_ambiguous`` over the ``k`` candidates). A model written by
``session.add(x)`` is known from ``x``'s assignment, so those edges are ``receiver_heuristic``.

What it writes (engine §11.2), all of it in the graph's ``LINKS`` layer:

- ``OrmModel``, ``Table`` and ``Column`` nodes (a column's table is ``attrs.table``);
  ``class -MAPS_TO-> OrmModel -MAPS_TO-> Table`` and ``field -MAPS_TO-> Column``: what the code
  declares, certain (``contract``);
- ``function -READS/WRITES-> Table`` for every query site (``attrs.model``, ``attrs.via``), the
  way a trace walks down to the data;
- a ``SqlStatement`` node per raw SQL string (its function is ``attrs.function``), and both
  of them ``-READS/WRITES-> Table``. The SQL names its tables exactly: a table the models or
  migrations declare is matched at 0.9; one only the SQL names gets a node of its own at
  ``name_unique``'s 0.4;
- tables the migrations create but no model maps get a node in their migration. When a
  repository has migrations, every model's table is checked against them: ``attrs.in_migrations``
  on the table, and the ones they don't create listed in :attr:`OrmReport.mismatches`.

Not yet: single-table inheritance (a subclass without its own table maps its parent's), a
``__tablename__`` computed by ``declared_attr``, schemas in ``__table_args__``, Django and
Prisma models.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any

from tree_sitter import Node

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.link.pytree import ancestors, string_value, walk
from sensei_engine.link.sql import looks_like_sql, tables
from sensei_engine.parse.parser import ParseResult
from sensei_engine.symbols.extract import Symbol, SymbolKind

MAX_SQL_ATTR = 2000  # of a statement's text, kept on its node
MAX_NAME = 80
KNOWN_TABLE_CONFIDENCE = 0.9  # raw SQL naming a table the models or migrations declare
_DECLARED = ("model", "migration", "table")  # where a table node's name can come from

_SESSION_WRITES = frozenset({"add", "add_all", "delete", "merge"})
_SESSION_READS = frozenset({"get", "get_one", "query"})
_STATEMENT_READS = frozenset({"select"})
_STATEMENT_WRITES = frozenset({"insert", "update", "delete"})
_CHAIN_READS = frozenset({"select_from", "join", "outerjoin"})
_CONSTRUCTORS = frozenset({"model_validate", "from_orm", "parse_obj", "model_construct"})
_SQL_FUNCTIONS = frozenset({"text"})
_SQL_METHODS = frozenset({"execute", "exec_driver_sql", "executemany", "executescript"})
_SQL_MODULES = frozenset({"sa", "sqlalchemy", "sqlmodel"})
_NOT_COLUMNS = frozenset(
    {
        "Relationship",
        "relationship",
        "backref",
        "association_proxy",
        "column_property",
        "synonym",
        "composite",
    }
)
_COLUMN_CALLS = frozenset({"Column", "mapped_column"})
_SKIPPED_FIELDS = frozenset({"model_config", "metadata", "registry", "__table_args__"})
_MIGRATION_OPS = frozenset({"create_table", "drop_table", "rename_table", "add_column"})
_OPTIONAL_RE = re.compile(r"^(?:Optional\[(.+)\]|(.+?)\s*\|\s*None|None\s*\|\s*(.+))$")
_ANNOTATED_RE = re.compile(r"^Annotated\[\s*([^,\]]+)")


@dataclass(frozen=True, slots=True)
class Import:
    module: str  # dotted, with the leading dots of a relative import ("..models")
    name: str | None  # the name imported from it (None: the module itself)


@dataclass(frozen=True, slots=True)
class FieldDecl:
    name: str
    symbol_key: str | None
    line: int
    annotated: bool
    call: str | None  # the call it's assigned (its last name: "Field", "mapped_column")
    column: str | None  # the column name the call gives, when it gives one
    mapped: bool  # annotated Mapped[...]
    excluded: bool  # a relationship, a ClassVar or private: never a column


@dataclass(slots=True)
class ClassDecl:
    name: str
    symbol_key: str | None
    path: str
    start_line: int
    end_line: int
    bases: tuple[str, ...]
    table_kw: bool  # class X(SQLModel, table=True)
    tablename: str | None  # a string __tablename__
    table_object: str | None  # __table__ = Table("name", ...)
    fields: list[FieldDecl] = field(default_factory=list[FieldDecl])

    @property
    def model(self) -> bool:
        return self.table_kw or self.tablename is not None or self.table_object is not None

    @property
    def table(self) -> str | None:
        if self.tablename is not None:
            return self.tablename
        if self.table_object is not None:
            return self.table_object
        return self.name.lower() if self.table_kw else None

    @property
    def framework(self) -> str:
        return "sqlmodel" if self.table_kw else "sqlalchemy"


@dataclass(frozen=True, slots=True)
class TableDecl:
    name: str
    variable: str | None  # what it's assigned to (users = Table("users", ...))
    path: str
    line: int
    columns: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Access:
    src: str  # the symbol key of the function it's in (the module's at module level)
    target: str  # the model's name as written ("Item", "models.Item"); "get_user()": what the
    # function of that name returns
    write: bool
    line: int
    via: str  # "select", "session.get", "session.add", ...
    receiver: bool  # the model is the type of the argument, not the argument itself


@dataclass(frozen=True, slots=True)
class Statement:
    src: str
    sql: str
    line: int
    via: str  # "text", "execute", ...


@dataclass(frozen=True, slots=True)
class MigrationOp:
    op: str  # create_table, drop_table, rename_table, add_column
    table: str
    line: int
    columns: tuple[tuple[str, int], ...] = ()
    renamed_to: str | None = None


@dataclass(slots=True)
class FileOrm:
    """What one Python file holds for the data layer."""

    path: str
    module_key: str
    imports: dict[str, Import] = field(default_factory=dict[str, Import])
    classes: list[ClassDecl] = field(default_factory=list[ClassDecl])
    tables: list[TableDecl] = field(default_factory=list[TableDecl])
    accesses: list[Access] = field(default_factory=list[Access])
    statements: list[Statement] = field(default_factory=list[Statement])
    migrations: list[MigrationOp] = field(default_factory=list[MigrationOp])
    # Module level: X = Annotated[Model, ...] (and other aliases of a name), and what each
    # function says it returns.
    aliases: dict[str, str] = field(default_factory=dict[str, str])
    returns: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def empty(self) -> bool:
        return not (self.classes or self.tables or self.accesses or self.statements) and not (
            self.migrations
        )


@dataclass(slots=True)
class OrmReport:
    models: int = 0
    tables: int = 0
    columns: int = 0
    accesses: int = 0  # query sites linked to a table
    unresolved: int = 0  # query sites whose model wasn't found
    statements: int = 0
    sql_errors: int = 0
    mismatches: list[str] = field(default_factory=list[str])  # model tables no migration makes


@dataclass(slots=True)
class OrmGraph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    report: OrmReport


# ---------------------------------------------------------------- scanning one file


def _last_name(node: Node | None, text: str) -> str:
    if node is None:
        return ""
    return text.rsplit(".", 1)[-1] if node.type in ("attribute", "identifier") else ""


def _optional(text: str) -> str:
    """The type in an annotation: ``X`` from ``X | None``, ``Optional[X]``, ``None | X``,
    ``Annotated[X, ...]`` and ``"X"``."""
    text = text.strip().strip("'\"")
    for _ in range(3):
        annotated = _ANNOTATED_RE.match(text)
        if annotated is not None:
            text = annotated[1].strip()
            continue
        found = _OPTIONAL_RE.match(text)
        if found is None:
            break
        text = next(group for group in found.groups() if group).strip().strip("'\"")
    return text


class _Scanner:
    def __init__(self, result: ParseResult, symbols: Sequence[Symbol]) -> None:
        self.result = result
        self.path = result.path
        module = next((s for s in symbols if s.kind is SymbolKind.MODULE), None)
        self.out = FileOrm(
            path=self.path, module_key=module.symbol_key if module else f"py {self.path}/"
        )
        self._callables = sorted(
            (s.start_line, s.end_line, s.symbol_key)
            for s in symbols
            if s.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
        )
        self._classes = [s for s in symbols if s.kind is SymbolKind.CLASS]
        self._fields = {
            (s.container_key, s.name): s.symbol_key
            for s in symbols
            if s.container_key is not None and s.attrs.get("subkind") == "field"
        }

    def text(self, node: Node) -> str:
        return self.result.text(node)

    def line(self, node: Node) -> int:
        return node.start_point.row + 1

    def enclosing(self, line: int) -> str:
        """The innermost function or method around ``line`` (else the module)."""
        found = self.out.module_key
        for start, end, key in self._callables:
            if start > line:
                break
            if line <= end:
                found = key  # sorted by start: a later one that contains it is inner
        return found

    def run(self) -> FileOrm:
        tree = self.result.tree
        if tree is None or not self.result.ok:
            return self.out
        for node in walk(tree.root_node):
            if node.type == "import_from_statement":
                self.import_from(node)
            elif node.type == "import_statement":
                self.import_module(node)
            elif node.type == "class_definition":
                self.class_definition(node)
            elif node.type == "function_definition":
                self.returns(node)
            elif node.type == "call":
                self.call(node)
        for statement in tree.root_node.named_children:
            assignment = statement.named_children[0] if statement.named_children else None
            if statement.type == "expression_statement" and assignment is not None:
                self.alias(assignment)
        return self.out

    def returns(self, node: Node) -> None:
        name, kind = node.child_by_field_name("name"), node.child_by_field_name("return_type")
        if name is not None and kind is not None:
            self.out.returns.setdefault(self.text(name), _optional(self.text(kind)))

    def alias(self, node: Node) -> None:
        """``CurrentUser = Annotated[User, Depends(...)]``, ``UserModel = User``."""
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if node.type != "assignment" or left is None or right is None or left.type != "identifier":
            return
        if right.type in ("identifier", "attribute") or (
            right.type == "subscript" and self.text(right).startswith("Annotated[")
        ):
            target = _optional(self.text(right))
            if target and target != self.text(left):
                self.out.aliases[self.text(left)] = target

    # imports

    def import_from(self, node: Node) -> None:
        module = node.child_by_field_name("module_name")
        if module is None:
            return
        source = self.text(module).replace(" ", "")
        for name in node.children_by_field_name("name"):
            if name.type == "aliased_import":
                original, alias = (
                    name.child_by_field_name("name"),
                    name.child_by_field_name("alias"),
                )
                if original is not None and alias is not None:
                    self.out.imports[self.text(alias)] = Import(source, self.text(original))
            else:
                self.out.imports[self.text(name)] = Import(source, self.text(name))

    def import_module(self, node: Node) -> None:
        for name in node.children_by_field_name("name"):
            if name.type == "aliased_import":
                original, alias = (
                    name.child_by_field_name("name"),
                    name.child_by_field_name("alias"),
                )
                if original is not None and alias is not None:
                    self.out.imports[self.text(alias)] = Import(self.text(original), None)
            else:
                dotted = self.text(name)  # import a.b: a.b is reachable as written
                self.out.imports[dotted] = Import(dotted, None)

    # classes

    def class_symbol(self, name: str, line: int) -> str | None:
        found = [s for s in self._classes if s.name == name and s.start_line <= line <= s.end_line]
        return max(found, key=lambda s: s.start_line).symbol_key if found else None

    def class_definition(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            return
        name = self.text(name_node)
        bases: list[str] = []
        table_kw = False
        superclasses = node.child_by_field_name("superclasses")
        for arg in superclasses.named_children if superclasses is not None else []:
            if arg.type == "keyword_argument":
                key, value = arg.child_by_field_name("name"), arg.child_by_field_name("value")
                if key is not None and value is not None and self.text(key) == "table":
                    table_kw = self.text(value) == "True"
            elif arg.type in ("identifier", "attribute"):
                bases.append(self.text(arg))
        start, end = self.line(node), node.end_point.row + 1
        decl = ClassDecl(
            name=name,
            symbol_key=self.class_symbol(name, start),
            path=self.path,
            start_line=start,
            end_line=end,
            bases=tuple(bases),
            table_kw=table_kw,
            tablename=None,
            table_object=None,
        )
        for statement in body.named_children:
            assignment = statement.named_children[0] if statement.named_children else None
            if statement.type != "expression_statement" or assignment is None:
                continue
            if assignment.type == "assignment":
                self.class_assignment(decl, assignment)
        self.out.classes.append(decl)

    def class_assignment(self, decl: ClassDecl, node: Node) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        kind = node.child_by_field_name("type")
        if left is None or left.type != "identifier":
            return
        name = self.text(left)
        if name == "__tablename__":
            value = self.string(right)
            decl.tablename = value if value else decl.tablename
            return
        if name == "__table__" and right is not None and right.type == "call":
            table = self.table_call(right, variable=None)
            decl.table_object = table.name if table else None
            return
        annotation = self.text(kind) if kind is not None else ""
        call = function = None
        column: str | None = None
        if right is not None and right.type == "call":
            function = right.child_by_field_name("function")
            call = _last_name(function, self.text(function)) if function is not None else None
            column = self.column_name(right, call)
        excluded = (
            name.startswith("_")
            or name in _SKIPPED_FIELDS
            or call in _NOT_COLUMNS
            or "ClassVar" in annotation
            or bool(re.search(r"\bMapped\[\s*(?:list|set|List|Set)\[", annotation))
        )
        key = self._fields.get((decl.symbol_key, name)) if decl.symbol_key else None
        decl.fields.append(
            FieldDecl(
                name=name,
                symbol_key=key,
                line=self.line(node),
                annotated=kind is not None,
                call=call,
                column=column,
                mapped=annotation.startswith("Mapped["),
                excluded=excluded,
            )
        )

    def column_name(self, call: Node, name: str | None) -> str | None:
        """The column name ``Column("name", ...)``/``mapped_column("name", ...)`` or
        ``Field(sa_column=Column("name", ...))`` gives, if any."""
        arguments = call.child_by_field_name("arguments")
        if arguments is None:
            return None
        if name in _COLUMN_CALLS:
            first = arguments.named_children[0] if arguments.named_children else None
            return (
                self.string(first)
                if first is not None and first.type != "keyword_argument"
                else None
            )
        if name == "Field":
            for arg in arguments.named_children:
                key, value = arg.child_by_field_name("name"), arg.child_by_field_name("value")
                if (
                    arg.type == "keyword_argument"
                    and key is not None
                    and value is not None
                    and self.text(key) == "sa_column"
                    and value.type == "call"
                ):
                    function = value.child_by_field_name("function")
                    inner = _last_name(function, self.text(function)) if function else None
                    return self.column_name(value, inner)
        return None

    # strings

    def string(self, node: Node | None) -> str | None:
        return string_value(self.result, node)

    # calls

    def args(self, call: Node) -> list[Node]:
        arguments = call.child_by_field_name("arguments")
        if arguments is None or arguments.type != "argument_list":
            return []
        return [a for a in arguments.named_children if a.type not in ("comment",)]

    def call(self, node: Node) -> None:
        function = node.child_by_field_name("function")
        if function is None:
            return
        text = self.text(function)
        line = self.line(node)
        if function.type == "identifier":
            self.function_call(node, text, line)
        elif function.type == "attribute":
            target = function.child_by_field_name("object")
            method = function.child_by_field_name("attribute")
            if target is None or method is None:
                return
            owner, name = self.text(target), self.text(method)
            if target.type == "identifier" and owner in _SQL_MODULES:
                self.function_call(node, name, line)
            elif target.type == "identifier" and owner == "op" and name in _MIGRATION_OPS:
                self.migration(node, name, line)
            else:
                self.method_call(node, owner, name, line)

    def table_call(self, node: Node, variable: str | None) -> TableDecl | None:
        args = self.args(node)
        name = self.string(args[0]) if args else None
        if not name:
            return None
        return TableDecl(
            name=name,
            variable=variable,
            path=self.path,
            line=self.line(node),
            columns=self.columns(args[1:]),
        )

    def columns(self, args: Iterable[Node]) -> tuple[tuple[str, int], ...]:
        found: list[tuple[str, int]] = []
        for arg in args:
            if arg.type != "call":
                continue
            function = arg.child_by_field_name("function")
            if function is None or _last_name(function, self.text(function)) != "Column":
                continue
            inner = self.args(arg)
            name = self.string(inner[0]) if inner else None
            if name:
                found.append((name, self.line(arg)))
        return tuple(found)

    def models_in(self, args: Iterable[Node]) -> Iterator[str]:
        """The model names among a statement's arguments (``Item``, ``models.Item``,
        ``Item.id`` → ``Item``)."""
        for arg in args:
            if arg.type == "identifier":
                yield self.text(arg)
            elif arg.type == "attribute":
                text = self.text(arg)
                head, _, last = text.rpartition(".")
                yield text if last[:1].isupper() else head

    def access(
        self, target: str, *, write: bool, line: int, via: str, receiver: bool = False
    ) -> None:
        if target and target[:1] != "_":
            self.out.accesses.append(
                Access(self.enclosing(line), target, write, line, via, receiver)
            )

    def function_call(self, node: Node, name: str, line: int) -> None:
        args = self.args(node)
        if name in _STATEMENT_READS or name in _STATEMENT_WRITES:
            write = name in _STATEMENT_WRITES
            for target in self.models_in(args[:1] if write else args):
                self.access(target, write=write, line=line, via=name)
        elif name == "Table":
            parent = node.parent
            variable = None
            if parent is not None and parent.type == "assignment":
                left = parent.child_by_field_name("left")
                variable = (
                    self.text(left) if left is not None and left.type == "identifier" else None
                )
            in_class = any(p.type == "class_definition" for p in ancestors(node))
            table = None if in_class else self.table_call(node, variable)
            if table is not None:
                self.out.tables.append(table)
        elif name in _SQL_FUNCTIONS:
            self.sql(args, line, name)

    def method_call(self, node: Node, owner: str, name: str, line: int) -> None:
        args = self.args(node)
        if name in _CHAIN_READS:
            for target in self.models_in(args[:1]):
                self.access(target, write=False, line=line, via=name)
        elif name in _SESSION_READS and args:
            for target in self.models_in(args[:1]):
                self.access(target, write=False, line=line, via=f"session.{name}")
        elif name in _SESSION_WRITES and args and _sessionish(owner):
            values = args[0].named_children if args[0].type in ("list", "tuple") else args[:1]
            for value in values:
                if value.type != "identifier":
                    continue
                model = self.type_of(self.text(value), node, line)
                if model:
                    self.access(model, write=True, line=line, via=f"session.{name}", receiver=True)
        elif name in _SQL_METHODS:
            self.sql(args, line, name)

    def sql(self, args: Sequence[Node], line: int, via: str) -> None:
        text = self.string(args[0]) if args else None
        if text and looks_like_sql(text):
            self.out.statements.append(Statement(self.enclosing(line), text, line, via))

    def migration(self, node: Node, op: str, line: int) -> None:
        args = self.args(node)
        table = self.string(args[0]) if args else None
        if not table:
            return
        renamed = self.string(args[1]) if op == "rename_table" and len(args) > 1 else None
        self.out.migrations.append(
            MigrationOp(op, table, line, columns=self.columns(args[1:]), renamed_to=renamed)
        )

    # receivers

    def type_of(self, variable: str, call: Node, line: int) -> str | None:
        """The model ``variable`` holds at ``line``, from its function: the last assignment
        before it (``x = M(...)``, ``x = M.model_validate(...)``, ``x = session.get(M, …)``,
        ``x: M = ...``), else its parameter's annotation."""
        function = next((p for p in ancestors(call) if p.type == "function_definition"), None)
        if function is None:
            return None
        found: str | None = None
        body = function.child_by_field_name("body")
        for node in walk(body) if body is not None else ():
            if node.type != "assignment" or self.line(node) >= line:
                continue
            left = node.child_by_field_name("left")
            if left is None or left.type != "identifier" or self.text(left) != variable:
                continue
            kind = node.child_by_field_name("type")
            right = node.child_by_field_name("right")
            assigned = self.constructed(right) if right is not None else None
            if kind is not None:
                assigned = _optional(self.text(kind))
            if assigned is not None:
                found = assigned
        if found is not None:
            return found
        parameters = function.child_by_field_name("parameters")
        for parameter in parameters.named_children if parameters is not None else []:
            if parameter.type not in ("typed_parameter", "typed_default_parameter"):
                continue
            name = parameter.child_by_field_name("name") or next(
                (c for c in parameter.named_children if c.type == "identifier"), None
            )
            kind = parameter.child_by_field_name("type")
            if name is not None and kind is not None and self.text(name) == variable:
                return _optional(self.text(kind))
        return None

    def constructed(self, node: Node) -> str | None:
        """The model a call's value is: ``M(...)``, ``M.model_validate(...)``,
        ``session.get(M, …)``; ``f()`` for what a function returns (resolved when linking)."""
        function = node.child_by_field_name("function") if node.type == "call" else None
        if function is not None and function.type == "identifier":
            name = self.text(function)
            return name if name[:1].isupper() else f"{name}()"
        if function is None or function.type != "attribute":
            return None
        target = function.child_by_field_name("object")
        method = function.child_by_field_name("attribute")
        if target is None or method is None:
            return None
        return self.method_value(node, target, self.text(method), self.text(function))

    def method_value(self, call: Node, target: Node, name: str, text: str) -> str | None:
        if name in _CONSTRUCTORS:
            return self.text(target)
        if name in ("get", "get_one"):
            return next(self.models_in(self.args(call)[:1]), None)
        if target.type != "identifier" or _sessionish(self.text(target)):
            return None
        return text if name[:1].isupper() else f"{text}()"  # models.Item(...), crud.get(...)


def _sessionish(owner: str) -> bool:
    last = owner.rsplit(".", 1)[-1].lower()
    return "session" in last or last in ("db", "s", "sess", "conn", "connection", "uow")


def scan(result: ParseResult, symbols: Sequence[Symbol]) -> FileOrm:
    """The data-layer facts in one parsed Python file (``symbols``: its definitions, from
    :func:`~sensei_engine.symbols.extract.extract_symbols`)."""
    if result.language != "python":
        return FileOrm(path=result.path, module_key=f"py {result.path}/")
    return _Scanner(result, symbols).run()


# ---------------------------------------------------------------- linking the files


def _modules(path: str) -> list[str]:
    """The dotted names a file can be imported as: every suffix of its path ("app.models",
    "models" for backend/app/models.py; a package's __init__ is the package)."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return [".".join(parts[i:]) for i in range(len(parts)) if parts[i:]]


def _relative(path: str, module: str) -> str | None:
    """``from ..x import y`` in ``path`` as a path prefix ("backend/app/x"), or None."""
    dots = len(module) - len(module.lstrip("."))
    if not dots:
        return None
    base = list(PurePosixPath(path).parent.parts)
    if dots - 1 > len(base):
        return None
    base = base[: len(base) - (dots - 1)]
    rest = [part for part in module.lstrip(".").split(".") if part]
    return "/".join(base + rest)


class _Linker:
    def __init__(self, files: Sequence[FileOrm]) -> None:
        self.files = {f.path: f for f in files}
        self.by_module: dict[str, set[str]] = defaultdict(set)
        for path in self.files:
            for module in _modules(path):
                self.by_module[module].add(path)
        self.classes: dict[tuple[str, str], ClassDecl] = {}
        self.models: dict[str, list[ClassDecl]] = defaultdict(list)
        self.table_vars: dict[tuple[str, str], TableDecl] = {}
        self.aliases = {(f.path, n): t for f in files for n, t in f.aliases.items()}
        self.returns = {(f.path, n): t for f in files for n, t in f.returns.items()}
        for f in files:
            for decl in f.classes:
                self.classes[(f.path, decl.name)] = decl
                if decl.model:
                    self.models[decl.name].append(decl)
            for table in f.tables:
                if table.variable:
                    self.table_vars[(f.path, table.variable)] = table
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self.report = OrmReport()

    # names

    def module_files(self, path: str, module: str) -> set[str]:
        relative = _relative(path, module)
        if relative is not None:
            return {p for p in (f"{relative}.py", f"{relative}/__init__.py") if p in self.files}
        return set(self.by_module.get(module, ()))

    def defines(self, path: str, name: str) -> bool:
        key = (path, name)
        return (
            key in self.classes
            or key in self.table_vars
            or key in self.aliases
            or key in self.returns
        )

    def locate(self, path: str, name: str) -> list[tuple[str, str]]:
        """Where ``name`` (as written in the file at ``path``) is defined, its imports
        followed: (file, the name there)."""
        imports = self.files[path].imports if path in self.files else {}
        head, _, rest = name.partition(".")
        if not rest:
            if self.defines(path, name):
                return [(path, name)]
            imported = imports.get(name)
            if imported is None or imported.name is None:
                return []
            module, attr = imported.module, imported.name
        else:
            imported = imports.get(head)
            if imported is None:
                return []
            module = imported.module + (f".{imported.name}" if imported.name else "")
            inner, _, attr = rest.rpartition(".")
            module = f"{module}.{inner}" if inner else module
        return [(p, attr) for p in sorted(self.module_files(path, module)) if self.defines(p, attr)]

    def resolve(
        self, path: str, name: str, *, models_only: bool, depth: int = 0
    ) -> list[tuple[Any, Provenance]]:
        """What ``name`` (as written in the file at ``path``) is: classes (or ``Table``
        objects), and how they were found. ``f()`` is what the function ``f`` returns."""
        if depth > 4:
            return []
        if name.endswith("()"):
            hits: list[tuple[Any, Provenance]] = []
            for where, function in self.locate(path, name[:-2]):
                returned = self.returns.get((where, function))
                if returned:
                    hits += self.resolve(where, returned, models_only=models_only, depth=depth + 1)
            return hits
        hits = []
        for where, attr in self.locate(path, name):
            decl = self.classes.get((where, attr))
            if decl is not None and (decl.model or not models_only):
                hits.append((decl, Provenance.IMPORT_RESOLVED))
            table = self.table_vars.get((where, attr))
            if table is not None:
                hits.append((table, Provenance.IMPORT_RESOLVED))
            alias = self.aliases.get((where, attr))
            if alias is not None and decl is None:
                hits += self.resolve(where, alias, models_only=models_only, depth=depth + 1)
        if hits:
            return hits
        imported = self.files[path].imports.get(name) if path in self.files else None
        wanted = imported.name if imported is not None and imported.name else name
        wanted = wanted.rsplit(".", 1)[-1]
        if not wanted[:1].isupper():
            return []  # a variable or a function: only a class's name is worth guessing
        candidates = (
            self.models.get(wanted, [])
            if models_only
            else [decl for (_, n), decl in self.classes.items() if n == wanted]
        )
        if len(candidates) == 1:
            return [(candidates[0], Provenance.NAME_UNIQUE)]
        return [(decl, Provenance.NAME_AMBIGUOUS) for decl in candidates]

    # nodes

    def node(self, node: GraphNode) -> str:
        if node.key not in self.nodes:
            self.nodes[node.key] = node
        return node.key

    def edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)

    def table(self, name: str, *, path: str | None, line: int, source: str) -> str:
        key = f"table {name.lower()}"
        existing = self.nodes.get(key)
        if existing is None:
            self.nodes[key] = GraphNode(
                key=key,
                kind=NodeKind.TABLE,
                name=name,
                path=path,
                start_line=line,
                end_line=line,
                lang="sql",
                attrs={"sources": [source]},
                owner=path,
            )
            self.report.tables += 1
        elif source not in existing.attrs.get("sources", []):
            sources = [*existing.attrs.get("sources", []), source]
            self.nodes[key] = _with_attrs(existing, sources=sources)
        return key

    def column(self, table_key: str, table: str, name: str, *, path: str, line: int) -> str:
        key = f"column {table.lower()}.{name}"
        if key not in self.nodes:
            self.nodes[key] = GraphNode(
                key=key,
                kind=NodeKind.COLUMN,
                name=name,
                path=path,
                start_line=line,
                end_line=line,
                qualified_name=f"{table}.{name}",
                lang="sql",
                attrs={"table": table_key},
                owner=path,
            )
            self.report.columns += 1
        return key

    # passes

    def run(self) -> OrmGraph:
        for decl in (d for f in self.files.values() for d in f.classes if d.model):
            self.model(decl)
        for table in (t for f in self.files.values() for t in f.tables):
            key = self.table(table.name, path=table.path, line=table.line, source="table")
            for name, line in table.columns:
                self.column(key, table.name, name, path=table.path, line=line)
        self.migrations()
        for f in self.files.values():
            for access in f.accesses:
                self.access(f.path, access)
            for statement in f.statements:
                self.statement(f.path, statement)
        return OrmGraph(list(self.nodes.values()), self.edges, self.report)

    def model(self, decl: ClassDecl) -> None:
        table = decl.table
        if table is None or decl.symbol_key is None:
            return
        key = self.node(
            GraphNode(
                key=f"orm_model {decl.symbol_key}",
                kind=NodeKind.ORM_MODEL,
                name=decl.name,
                path=decl.path,
                start_line=decl.start_line,
                end_line=decl.end_line,
                lang="python",
                attrs={"framework": decl.framework, "table": table, "symbol": decl.symbol_key},
                owner=decl.path,
            )
        )
        self.report.models += 1
        table_key = self.table(table, path=decl.path, line=decl.start_line, source="model")
        named = "explicit" if decl.tablename or decl.table_object else "class name"
        self.edge(
            GraphEdge(
                decl.symbol_key,
                key,
                EdgeType.MAPS_TO,
                Provenance.CONTRACT,
                owner=decl.path,
                evidence_path=decl.path,
                evidence_line=decl.start_line,
            )
        )
        self.edge(
            GraphEdge(
                key,
                table_key,
                EdgeType.MAPS_TO,
                Provenance.CONTRACT,
                owner=decl.path,
                attrs={"named": named},
                evidence_path=decl.path,
                evidence_line=decl.start_line,
            )
        )
        for field_decl in self.columns_of(decl):
            column = field_decl.column or field_decl.name
            column_key = self.column(table_key, table, column, path=decl.path, line=field_decl.line)
            if field_decl.symbol_key is not None:
                self.edge(
                    GraphEdge(
                        field_decl.symbol_key,
                        column_key,
                        EdgeType.MAPS_TO,
                        Provenance.CONTRACT,
                        evidence_path=decl.path,
                        evidence_line=field_decl.line,
                        owner=decl.path,
                    )
                )

    def columns_of(self, decl: ClassDecl) -> list[FieldDecl]:
        """The model's own column fields, then those of the non-table classes it inherits."""
        found: dict[str, FieldDecl] = {}
        seen: set[tuple[str, str]] = set()

        def visit(current: ClassDecl, depth: int) -> None:
            if (current.path, current.name) in seen or depth > 8:
                return
            seen.add((current.path, current.name))
            for f in current.fields:
                if f.name not in found and self.is_column(decl, f):
                    found[f.name] = f
            for base in current.bases:
                for parent, _ in self.resolve(current.path, base, models_only=False):
                    if isinstance(parent, ClassDecl) and not parent.model:
                        visit(parent, depth + 1)

        visit(decl, 0)
        return sorted(found.values(), key=lambda f: (f.line, f.name))

    @staticmethod
    def is_column(model: ClassDecl, f: FieldDecl) -> bool:
        if f.excluded:
            return False
        if f.call in _COLUMN_CALLS or f.mapped:
            return True
        return model.table_kw and (f.annotated or f.call == "Field")

    def migrations(self) -> None:
        created: dict[str, tuple[str, MigrationOp]] = {}
        renamed: set[str] = set()
        for f in self.files.values():
            for op in f.migrations:
                if op.op == "create_table":
                    created.setdefault(op.table.lower(), (f.path, op))
                elif op.op == "rename_table" and op.renamed_to:
                    created.setdefault(op.renamed_to.lower(), (f.path, op))
                    renamed.add(op.table.lower())
        for name in renamed - {n for n, (_, op) in created.items() if op.renamed_to}:
            created.pop(name, None)  # renamed away (and not renamed back)
        if not created:
            return
        for key, node in list(self.nodes.items()):
            if node.kind is not NodeKind.TABLE:
                continue
            known = node.name.lower() in created
            self.nodes[key] = _with_attrs(node, in_migrations=known)
            if not known:
                self.report.mismatches.append(node.name)
        for path, op in created.values():
            name = op.renamed_to or op.table
            key = self.table(name, path=path, line=op.line, source="migration")
            if key in self.nodes and self.nodes[key].owner == path:
                self.nodes[key] = _with_attrs(self.nodes[key], in_migrations=True)
                for column, line in op.columns:
                    self.column(key, name, column, path=path, line=line)

    def access(self, path: str, access: Access) -> None:
        found = self.resolve(path, access.target, models_only=True)
        if not found:
            self.report.unresolved += 1
            return
        for target, provenance in found:
            if isinstance(target, TableDecl):
                name, model = target.name, None
            else:
                name, model = target.table, f"orm_model {target.symbol_key}"
            if name is None:
                continue
            how = provenance
            if access.receiver and provenance is Provenance.IMPORT_RESOLVED:
                how = Provenance.RECEIVER_HEURISTIC
            attrs: dict[str, Any] = {"via": access.via}
            if model is not None:
                attrs["model"] = model
            self.edge(
                GraphEdge(
                    access.src,
                    f"table {name.lower()}",
                    EdgeType.WRITES if access.write else EdgeType.READS,
                    how,
                    confidence=confidence_of(how, len(found)),
                    evidence_path=path,
                    evidence_line=access.line,
                    attrs=attrs,
                    owner=path,
                )
            )
        self.report.accesses += 1

    def statement(self, path: str, statement: Statement) -> None:
        parsed = tables(statement.sql)
        key = self.node(
            GraphNode(
                key=f"sql_statement {path}:{statement.line}",
                kind=NodeKind.SQL_STATEMENT,
                name=" ".join(statement.sql.split())[:MAX_NAME],
                path=path,
                start_line=statement.line,
                end_line=statement.line,
                lang="sql",
                attrs={
                    "kind": parsed.kind,
                    "sql": statement.sql[:MAX_SQL_ATTR],
                    "via": statement.via,
                    "function": statement.src,
                    **({"error": parsed.error} if parsed.error else {}),
                },
                owner=path,
            )
        )
        self.report.statements += 1
        if parsed.error:
            self.report.sql_errors += 1
        line = statement.line
        for names, kind in ((parsed.reads, EdgeType.READS), (parsed.writes, EdgeType.WRITES)):
            for name in names:
                table_key, known = self.sql_table(name, statement.line)
                confidence = (
                    KNOWN_TABLE_CONFIDENCE if known else confidence_of(Provenance.NAME_UNIQUE)
                )
                for src in (key, statement.src):
                    self.edge(
                        GraphEdge(
                            src,
                            table_key,
                            kind,
                            Provenance.NAME_UNIQUE,
                            confidence=confidence,
                            evidence_path=path,
                            evidence_line=line,
                            attrs={"sql": key},
                            owner=path,
                        )
                    )

    def sql_table(self, name: str, line: int) -> tuple[str, bool]:
        """The table node SQL's ``name`` means, and whether a model or migration declares it.
        A schema-qualified name matches a declared table of its last part."""
        for candidate in (name, name.rsplit(".", 1)[-1]):
            node = self.nodes.get(f"table {candidate.lower()}")
            if node is not None:
                declared = any(s in node.attrs.get("sources", []) for s in _DECLARED)
                return self.table(node.name, path=node.path, line=line, source="sql"), declared
        return self.table(name, path=None, line=0, source="sql"), False


def _with_attrs(node: GraphNode, **attrs: Any) -> GraphNode:
    return replace(node, attrs={**node.attrs, **attrs})


def link(files: Iterable[FileOrm]) -> OrmGraph:
    """The snapshot's data-layer graph, from every Python file's :func:`scan`."""
    return _Linker([f for f in files if not f.empty]).run()
