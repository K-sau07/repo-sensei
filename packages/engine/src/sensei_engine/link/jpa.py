"""JPA entities, Spring Data repositories, the tables they map and the code that reads and writes
them (3.20), for Java: what :mod:`sensei_engine.link.orm` is for Python, in the same shapes.

Two passes. :func:`scan` reads one parsed Java file in the parse pool: its facts as the Java
resolver extracts them (:mod:`sensei_engine.graph.java.facts`), the fields it declares
``transient`` and the first argument of the calls that may be an ``EntityManager``'s, a
Hibernate ``Session``'s or a ``JdbcTemplate``'s (a class literal, a name, a string).
:func:`scan_text` reads the other files JPA needs: Flyway's migrations (``V1__init.sql``,
``R__views.sql``) and a ``schema.sql`` through :func:`sensei_engine.link.sql.ddl`, Liquibase's
changelogs (XML and YAML: ``createTable``, ``renameTable``, ``dropTable``) and Spring's
``application*.properties``/``.yml`` for the physical naming strategy.

:func:`link` then resolves them over one :class:`~sensei_engine.graph.java.imports.TypeIndex` of
the repository's types, as the graph stage's Java resolver does, and its
:class:`~sensei_engine.graph.java.calls.CallResolver` for receivers:

- an **entity** is a class with ``@Entity`` (``jakarta.persistence`` or ``javax.persistence``);
  its name is ``@Entity(name = ...)``, else the class's; its table ``@Table(name = ...)`` (and
  ``schema``), else the entity name, through the **physical naming strategy**. Spring Boot's
  default (``CamelCaseToUnderscoresNamingStrategy``, ``SpringPhysicalNamingStrategy`` before
  it) puts an underscore between a lower-case letter or digit and an upper-case letter followed
  by a lower-case one, turns dots into underscores and lower-cases the rest (``OrderLine`` →
  ``order_line``), explicit names included; a config naming Hibernate's
  ``PhysicalNamingStrategyStandardImpl`` keeps names as written (:data:`NAMINGS`). An entity
  extending another one maps the root's table under ``SINGLE_TABLE`` (the default
  ``@Inheritance``), its own under ``JOINED`` and ``TABLE_PER_CLASS``;
- its **columns** are its instance fields but ``@Transient``, ``transient``, ``@Formula`` ones and
  the relationships: ``@OneToMany``, ``@ManyToMany``, ``@ElementCollection`` and an inverse
  ``@OneToOne(mappedBy = ...)`` aren't columns; an owning ``@ManyToOne``/``@OneToOne`` is its
  ``@JoinColumn(name = ...)``, else ``<field>_<the target's id column>``. A column is
  ``@Column(name = ...)``, else the field's name, through the naming strategy. The fields of an
  ``@Embedded``/``@EmbeddedId`` (or ``@Embeddable``-typed) field and of the ``@MappedSuperclass``
  classes it extends are its own (as orm.py takes a non-table base class's); a plain base
  class's aren't (JPA doesn't map them). A ``@JoinTable(name = ...)`` is a table with its join
  columns. A Spring Data MongoDB ``@Document`` is a collection: a table with ``attrs.store``
  ``mongo``, named ``collection``, else the class's name uncapitalized, its columns the field
  names (``@Field``'s, ``_id`` for the id);
- a **repository** is an interface extending a Spring Data repository (``JpaRepository``,
  ``CrudRepository``, ``MongoRepository``, ...: Java's ``attrs.REPOSITORIES``), directly or
  through interfaces of the repository's own (``BaseRepository<T, ID>``): its entity is the
  first type argument, followed through them;
- its **methods** read and write its entity's table: ``@Query`` JPQL names entities (its
  ``FROM``, ``JOIN``, ``UPDATE``, ``DELETE FROM`` and ``INSERT INTO``; ``#{#entityName}`` is the
  repository's), ``@Query(nativeQuery = true)`` names tables (:mod:`sensei_engine.link.sql`,
  ``?1`` parameters read as ``?``), an ``UPDATE``/``DELETE`` writes (``@Modifying`` too);
  without one, a derived name (Spring Data's ``PartTree``: ``find|read|get|query|search|stream|
  count|exists…By`` read, ``delete|remove…By`` write) or a method it redeclares (``save``,
  ``findAll``: :data:`INHERITED_WRITES`, :data:`INHERITED_READS`);
- a **call of a method a repository inherits** from the library (``userRepository.save(u)``:
  the Java resolver's edge goes to the library's method, its ``attrs.receiver`` the repository)
  reads or writes the table from the calling function;
- an ``EntityManager``'s (or ``Session``'s) ``persist/merge/remove`` write the entity its
  argument is, ``find/getReference`` read the class literal's, ``createQuery`` reads (or writes)
  what its JPQL names and ``createNativeQuery`` what its SQL does; a ``JdbcTemplate``'s SQL is
  raw SQL.

What it writes, all of it in the ``LINKS`` layer, as orm.py does: ``OrmModel`` (``framework``
``jpa`` or ``mongo``), ``Table`` and ``Column`` nodes, ``class -MAPS_TO-> OrmModel -MAPS_TO->
Table`` and ``field -MAPS_TO-> Column`` (``contract``), ``repository -MAPS_TO-> Table``
(``import_resolved``: its type argument), ``SqlStatement`` nodes for native SQL, and
``READS``/``WRITES`` edges to tables (``attrs.model``, ``attrs.via``) from repository methods
and the code that calls the library's. How sure: a declared ``@Query`` naming the entity or a
declared table is certain (``contract``); a derived or redeclared method of a typed repository
``import_resolved``; an inherited call resolved through its receiver's type and an
``EntityManager`` call's argument by its variable ``receiver_heuristic``; SQL naming a table
nothing declares ``name_unique`` (as orm.py). Tables the migrations create are checked against
the entities' (``attrs.in_migrations``, :attr:`JpaReport.mismatches`), as orm.py checks
Alembic's.

Not yet: property access (annotations on getters), ``@AttributeOverride``, ``@SecondaryTable``,
``@NamedQuery``, derived queries' joins through a property path (``findByCustomerName`` reads
the customer's table too), a naming strategy of the repository's own (Spring's default is
assumed), Spring Data JDBC's and R2DBC's ``@Table`` without ``@Entity``, a ``MongoTemplate``'s
queries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from posixpath import basename
from typing import Any, Literal

from tree_sitter import Node

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph import java
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.java.attrs import REPOSITORIES
from sensei_engine.graph.java.calls import CallResolver
from sensei_engine.graph.java.facts import Annotation, Call, JavaFacts, Receiver, Variable
from sensei_engine.graph.java.imports import (
    Declared,
    TypeIndex,
    TypeRef,
    base_name,
    type_arguments,
)
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.link.sql import TableChange, ddl, looks_like_sql, tables
from sensei_engine.parse.parser import ParseResult

TEXT_LANGUAGES = frozenset({"sql", "xml", "yaml", "properties"})
LANGUAGES = frozenset({"java", *TEXT_LANGUAGES})
MAX_SQL_ATTR = 2000
MAX_NAME = 80
MAX_DEPTH = 8
KNOWN_TABLE_CONFIDENCE = 0.9  # SQL in code naming a declared table (orm.py's)
_DECLARED = ("model", "migration", "table", "entity")

PERSISTENCE = ("jakarta.persistence.", "javax.persistence.")
MONGO_DOCUMENT = "org.springframework.data.mongodb.core.mapping.Document"
# Spring Data's CrudRepository/ListCrudRepository/JpaRepository/MongoRepository/... methods.
INHERITED_WRITES = frozenset({
    "save", "saveAll", "saveAndFlush", "saveAllAndFlush", "delete", "deleteById", "deleteAll",
    "deleteAllById", "deleteAllInBatch", "deleteInBatch", "deleteAllByIdInBatch", "flush",
    "insert",
})  # fmt: skip
INHERITED_READS = frozenset({
    "findById", "findAll", "findAllById", "existsById", "count", "exists", "findOne", "findBy",
    "getReferenceById", "getById", "getOne",
})  # fmt: skip
_DERIVED_RE = re.compile(
    r"^(find|read|get|query|search|stream|count|exists|delete|remove)"
    r"(?:(?:[A-Z]\w*?)?By[A-Z(]|(?:All|Distinct|First\d*|Top\d*)*$)"
)
_DERIVED_WRITES = frozenset({"delete", "remove"})
_NOT_COLUMNS = frozenset({"Transient", "Formula", "JoinFormula", "ElementCollection"})
_TO_MANY = frozenset({"OneToMany", "ManyToMany"})
_TO_ONE = frozenset({"ManyToOne", "OneToOne"})
_EMBEDDED = frozenset({"Embedded", "EmbeddedId"})
_JOIN_COLUMN_RE = re.compile(r"^@?\s*(?:[\w.]*\.)?JoinColumn\s*(\(.*\))\s*$", re.DOTALL)

# Calls whose first argument :func:`scan` keeps, and what each does on the types it serves.
_MANAGERS = frozenset({
    "jakarta.persistence.EntityManager", "javax.persistence.EntityManager",
    "org.hibernate.Session", "org.hibernate.StatelessSession",
})  # fmt: skip
_JDBC = frozenset({
    "org.springframework.jdbc.core.JdbcTemplate", "org.springframework.jdbc.core.JdbcOperations",
    "org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate",
    "org.springframework.jdbc.core.namedparam.NamedParameterJdbcOperations",
    "org.springframework.jdbc.core.simple.JdbcClient",
})  # fmt: skip
_MANAGER_WRITES = frozenset({
    "persist", "merge", "remove", "save", "saveOrUpdate", "update", "delete", "insert",
})  # fmt: skip
_MANAGER_READS = frozenset({"find", "getReference", "get", "load", "refresh"})
_MANAGER_JPQL = frozenset({"createQuery", "createSelectionQuery", "createMutationQuery"})
_MANAGER_SQL = frozenset({"createNativeQuery", "createSQLQuery", "createNativeMutationQuery"})
_JDBC_SQL = frozenset({
    "query", "queryForObject", "queryForList", "queryForMap", "queryForRowSet", "queryForStream",
    "update", "batchUpdate", "execute", "sql",
})  # fmt: skip
ARGUMENT_CALLS = _MANAGER_WRITES | _MANAGER_READS | _MANAGER_JPQL | _MANAGER_SQL | _JDBC_SQL

# The physical naming strategies a config may name: Spring's (the default) or Hibernate's
# standard one (names as written).
NAMINGS = {
    "PhysicalNamingStrategyStandardImpl": "standard",
    "CamelCaseToUnderscoresNamingStrategy": "spring",
    "SpringPhysicalNamingStrategy": "spring",
}
# At a line's start (after its indent and the key's dotted prefix), so a commented-out one
# (``#`` / ``!``) never counts.
_NAMING_RE = re.compile(
    r"^[ \t]*(?:[\w.\[\]-]*\.)?(?:physical[-_]strategy|physical_naming_strategy)"
    r"\s*[:=]\s*[\"']?([\w.$]+)",
    re.MULTILINE,
)
_FLYWAY_RE = re.compile(r"^(?:V\d[\w.]*|R)__.+\.sql$")
_LIQUIBASE_RE = re.compile(r"changelog|changeset", re.IGNORECASE)

Tool = Literal["flyway", "liquibase", "schema"]


# ---------------------------------------------------------------- per file


@dataclass(frozen=True, slots=True)
class Argument:
    """A call's first argument (kept for the ones :data:`ARGUMENT_CALLS` names)."""

    line: int  # the called name's (the Java resolver's call line)
    method: str
    kind: Literal["class", "name", "string", "new"]
    value: str  # a type as written, a name chain ("this.user"), a string's value
    column: int = 0  # the called name's (bytes, as the call's): two calls on a line apart


@dataclass(frozen=True, slots=True)
class FileJpa:
    """What one file holds for JPA."""

    path: str
    java: JavaFacts | None = None
    transient: frozenset[str] = frozenset()  # keys of the fields declared transient
    arguments: tuple[Argument, ...] = ()
    tool: Tool | None = None  # a migration's
    migrations: tuple[TableChange, ...] = ()
    naming: str | None = None  # the physical naming strategy a config names (its class)


def _sql_tool(path: str) -> Tool | None:
    name = basename(path)
    lowered = path.lower()
    if name.startswith("U") and "__" in name:
        return None  # Flyway's undo migrations don't run forward
    if _FLYWAY_RE.match(name) or "db/migration" in lowered:
        return "flyway"
    if _LIQUIBASE_RE.search(lowered):
        return "liquibase"
    if re.fullmatch(r"schema(?:-[\w-]+)?\.sql", name) and "/test/" not in f"/{lowered}":
        return "schema"
    return None


def text_file(path: str) -> Tool | Literal["config"] | None:
    """What a file that isn't Java is to JPA (None: nothing)."""
    name = basename(path)
    lowered = path.lower()
    if name.endswith(".sql"):
        return _sql_tool(path)
    if name.endswith((".xml", ".yaml", ".yml")) and _LIQUIBASE_RE.search(lowered):
        return "liquibase"
    if name.startswith(("application", "bootstrap")) and name.endswith(
        (".properties", ".yml", ".yaml")
    ):
        return "config"
    return None


def _config_rank(path: str) -> tuple[bool, bool, str]:
    """Which config's naming strategy holds, first first: the app's own (``src/main``, not a
    test's) before a profile's (``application-local.yml`` applies only under that profile)."""
    lowered = f"/{path.lower()}"
    test = "/test/" in lowered or "/tests/" in lowered
    profile = "-" in basename(path).split(".", 1)[0]
    return (test, profile, path)


def wanted(path: str, language: str | None) -> bool:
    """Whether JPA reads a file of a language it reads (a text file: only the ones it knows)."""
    return language not in TEXT_LANGUAGES or text_file(path) is not None


def scan_text(path: str, data: bytes) -> FileJpa:
    """A migration's table changes, or the naming strategy a Spring config names."""
    kind = text_file(path)
    text = data.decode("utf-8", "replace")
    if kind == "config":
        found = _NAMING_RE.search(text)
        return FileJpa(path, naming=found[1] if found else None)
    if kind is None:
        return FileJpa(path)
    if path.endswith(".sql"):
        return FileJpa(path, tool=kind, migrations=tuple(ddl(text)))
    if "databaseChangeLog" not in text:
        return FileJpa(path)
    changes = _liquibase_xml(text) if path.endswith(".xml") else _liquibase_yaml(text)
    return FileJpa(path, tool="liquibase", migrations=tuple(changes))


_XML_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*["']([^"']*)["']""")
# A change's body ends at its closing tag, never past the next change (so an unclosed one
# costs its own stretch of the file, not the rest of it each time).
_XML_CHANGE_RE = re.compile(
    r"<(createTable|renameTable|dropTable)\b([^>]*?)"
    r"(?:/>|>((?:(?!<(?:createTable|renameTable|dropTable)\b).)*?)</\1>)",
    re.DOTALL,
)
_XML_COLUMN_RE = re.compile(r"<column\b([^>]*)>?", re.DOTALL)


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _liquibase_xml(text: str) -> list[TableChange]:
    changes: list[TableChange] = []
    for found in _XML_CHANGE_RE.finditer(text):
        attrs = dict(_XML_ATTR_RE.findall(found[2]))
        line = _line_at(text, found.start())
        if found[1] == "createTable" and attrs.get("tableName"):
            columns = tuple(
                (name, _line_at(text, found.start(3) + column.start()))
                for column in _XML_COLUMN_RE.finditer(found[3] or "")
                if (name := dict(_XML_ATTR_RE.findall(column[1])).get("name"))
            )
            changes.append(TableChange("create_table", attrs["tableName"], line, columns))
        elif found[1] == "renameTable" and attrs.get("oldTableName") and attrs.get("newTableName"):
            changes.append(
                TableChange(
                    "rename_table", attrs["oldTableName"], line, renamed_to=attrs["newTableName"]
                )
            )
        elif found[1] == "dropTable" and attrs.get("tableName"):
            changes.append(TableChange("drop_table", attrs["tableName"], line))
    return changes


_YAML_KEY_RE = re.compile(r"^\s*(?:-\s+)?(\w+)\s*:")


def _yaml_key(line: str) -> tuple[str, str] | None:
    """A YAML line's key and its value (a comment after it dropped, quotes taken off), read by
    hand: a pattern for the value backtracks on long runs of spaces."""
    head = _YAML_KEY_RE.match(line)
    if head is None:
        return None
    value = line[head.end() :].split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1]
    return head[1], value


def _liquibase_yaml(text: str) -> list[TableChange]:
    """``createTable``/``renameTable``/``dropTable`` blocks of a YAML changelog: the keys
    indented under each."""
    lines = text.splitlines()
    changes: list[TableChange] = []
    for number, line in enumerate(lines):
        head = _yaml_key(line)
        if head is None or head[0] not in ("createTable", "renameTable", "dropTable"):
            continue
        indent = len(line) - len(line.lstrip(" -"))
        keys: dict[str, str] = {}
        columns: list[tuple[str, int]] = []
        for at in range(number + 1, len(lines)):
            inner = lines[at]
            if inner.strip() and len(inner) - len(inner.lstrip(" -")) <= indent:
                break
            found = _yaml_key(inner)
            if found is None or not found[1]:
                continue
            if found[0] == "name":
                columns.append((found[1], at + 1))
            else:
                keys.setdefault(found[0], found[1])
        if head[0] == "createTable" and keys.get("tableName"):
            changes.append(
                TableChange("create_table", keys["tableName"], number + 1, tuple(columns))
            )
        elif head[0] == "renameTable" and keys.get("oldTableName") and keys.get("newTableName"):
            changes.append(
                TableChange(
                    "rename_table",
                    keys["oldTableName"],
                    number + 1,
                    renamed_to=keys["newTableName"],
                )
            )
        elif head[0] == "dropTable" and keys.get("tableName"):
            changes.append(TableChange("drop_table", keys["tableName"], number + 1))
    return changes


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node | None) -> str:
    return (node.text or b"").decode("utf-8", "replace") if node is not None else ""


def _argument(node: Node) -> tuple[Literal["class", "name", "string", "new"], str] | None:
    while node.type == "parenthesized_expression" and node.named_children:
        node = node.named_children[0]
    if node.type in ("string_literal", "binary_expression"):
        value = string_expression(_text(node))
        return ("string", value) if value is not None else None
    if node.type == "class_literal":
        return "class", _text(node).rsplit(".class", 1)[0].strip()
    if node.type == "identifier":
        return "name", _text(node)
    if node.type == "field_access":
        return "name", "".join(_text(node).split())
    if node.type == "object_creation_expression":
        return "new", _text(node.child_by_field_name("type"))
    return None


def scan(result: ParseResult) -> FileJpa | None:
    """A Java file's facts, its ``transient`` fields and the first arguments of its calls that
    may be an ``EntityManager``'s or a ``JdbcTemplate``'s (None: another language, or no
    parse)."""
    facts = java.extract(result)
    if facts is None or result.tree is None:
        return None
    transient: set[tuple[int, str]] = set()
    arguments: list[Argument] = []
    for node in _walk(result.tree.root_node):
        if node.type == "field_declaration":
            modifiers = next((c for c in node.children if c.type == "modifiers"), None)
            if modifiers is not None and any(c.type == "transient" for c in modifiers.children):
                for declarator in node.children_by_field_name("declarator"):
                    name = _text(declarator.child_by_field_name("name"))
                    transient.add((node.start_point.row + 1, name))
        elif node.type == "method_invocation":
            name = node.child_by_field_name("name")
            if name is None or _text(name) not in ARGUMENT_CALLS:
                continue
            listed = node.child_by_field_name("arguments")
            first = next(
                (c for c in (listed.named_children if listed else ()) if "comment" not in c.type),
                None,
            )
            found = _argument(first) if first is not None else None
            if found is not None:
                row, column = name.start_point
                arguments.append(Argument(row + 1, _text(name), *found, column=column))
    keys = frozenset(
        f.key
        for t in facts.types
        for f in t.fields
        if f.key is not None and (f.line, f.name) in transient
    )
    return FileJpa(result.path, facts, keys, tuple(arguments))


# ---------------------------------------------------------------- Java source text


_STRING_RE = re.compile(r'"""(.*?)"""|"((?:[^"\\]|\\.)*)"', re.DOTALL)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "s": " ", "0": "\0"}


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m[1], m[1]), text, flags=re.DOTALL)


def string_expression(text: str) -> str | None:
    """The value of a Java string expression made of literals and text blocks joined with
    ``+`` (None: anything else is in it, a constant's name, a call)."""
    parts: list[str] = []
    position = 0
    text = text.strip()
    while position < len(text):
        while position < len(text) and (text[position].isspace() or text[position] == "+"):
            position += 1
        if position >= len(text):
            break
        found = _STRING_RE.match(text, position)
        if found is None:
            return None
        if found[1] is not None:  # a text block: its lines, their common indent removed
            lines = found[1].split("\n")  # (on one line: as the facts keep annotations)
            lines = lines[1:] if len(lines) > 1 and not lines[0].strip() else lines
            indent = min(
                (len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0
            )
            parts.append(_unescape("\n".join(line[indent:] for line in lines)))
        else:
            parts.append(_unescape(found[2]))
        position = found.end()
    return "".join(parts) if parts else None


def elements(arguments: str | None) -> dict[str, str]:
    """An annotation's element-value pairs, its source as written (``(name = "users")``;
    ``("x")`` is ``value``)."""
    if not arguments:
        return {}
    inner = arguments.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    found: dict[str, str] = {}
    for part in _split_top(inner):
        named = re.match(r"\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", part, re.DOTALL)
        if named is not None:
            found[named[1]] = named[2].strip()
        elif part.strip():
            found.setdefault("value", part.strip())
    return found


def _split_top(text: str) -> list[str]:
    """``text`` split at its commas outside strings, parentheses and braces."""
    parts: list[str] = []
    depth, start, i = 0, 0, 0
    while i < len(text):
        if text.startswith('"""', i):
            end = text.find('"""', i + 3)
            i = len(text) if end < 0 else end + 3
            continue
        char = text[i]
        if char == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _join_columns(arguments: str | None) -> list[str]:
    """The column names a ``@JoinTable``'s ``joinColumns`` and ``inverseJoinColumns`` give (one
    ``@JoinColumn(...)`` or ``{...}`` of them; ``name`` wherever it is among the elements)."""
    found: list[str] = []
    held = elements(arguments)
    for key in ("joinColumns", "inverseJoinColumns"):
        value = held.get(key, "").strip()
        if value.startswith("{") and value.endswith("}"):
            value = value[1:-1]
        for part in _split_top(value):
            column = _JOIN_COLUMN_RE.match(part.strip())
            name = _string(column[1], "name", "value") if column is not None else None
            if name:
                found.append(name)
    return found


def _string(arguments: str | None, *keys: str) -> str | None:
    found = elements(arguments)
    for key in keys:
        if key in found:
            return string_expression(found[key])
    return None


def physical(name: str, naming: str = "spring") -> str:
    """A logical name as the physical naming strategy makes it (see the module): Spring's
    ``OrderLine`` is ``order_line``; a quoted name is kept as written."""
    if len(name) > 1 and name[0] in '`"' and name[-1] == name[0]:
        return name[1:-1]
    if naming == "standard":
        return name
    chars = list(name.replace(".", "_"))
    i = 1
    while i < len(chars) - 1:
        before, current, after = chars[i - 1], chars[i], chars[i + 1]
        if (
            (before.islower() or before.isdigit())
            and current.isupper()
            and (after.islower() or after.isdigit())
        ):
            chars.insert(i, "_")
            i += 1
        i += 1
    return "".join(chars).lower()


# ---------------------------------------------------------------- JPQL


_JPQL_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_JPQL_FROM_RE = re.compile(
    r"\bfrom\s+(.+?)(?=\b(?:where|join|left|right|inner|outer|cross|group|order|having|union|"
    r"set)\b|\)|$)",
    re.IGNORECASE | re.DOTALL,
)
_JPQL_JOIN_RE = re.compile(r"\bjoin\s+(?:fetch\s+)?([\w.$]+)", re.IGNORECASE)
_JPQL_TARGET_RE = re.compile(
    r"^\s*(?:update\s+|delete\s+(?:from\s+)?|insert\s+into\s+)([\w.$]+)", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Jpql:
    kind: str  # select, update, delete, insert ("" when it reads as none)
    names: tuple[str, ...]  # the entity names it reads (paths and aliases among them)
    target: str | None  # the one it writes


def jpql(query: str) -> Jpql:
    """The entity names a JPQL (or HQL) query names, and the one it writes."""
    text = _JPQL_STRING_RE.sub("''", query)
    first = text.split(None, 1)[0].lower() if text.split() else ""
    kind = first if first in ("select", "update", "delete", "insert") else ""
    if first == "from":
        kind = "select"
    target = _JPQL_TARGET_RE.match(text)
    names: list[str] = []
    for clause in _JPQL_FROM_RE.finditer(text):
        for item in _split_top(clause[1]):
            words = item.split()
            if words and re.fullmatch(r"[A-Za-z_$][\w.$]*", words[0]):
                names.append(words[0])
    names += [m[1] for m in _JPQL_JOIN_RE.finditer(text)]
    written = target[1] if target is not None and kind != "select" else None
    return Jpql(kind, tuple(dict.fromkeys(n for n in names if n != written)), written)


# ---------------------------------------------------------------- linking


@dataclass(slots=True)
class JpaReport:
    entities: int = 0  # @Entity classes mapped to a table
    documents: int = 0  # MongoDB @Document classes
    tables: int = 0  # table nodes added
    columns: int = 0
    repositories: int = 0  # Spring Data repositories whose entity is the repository's
    methods: int = 0  # repository methods linked to a table
    derived: int = 0  # ... by their name
    queries: int = 0  # ... by a JPQL @Query
    native: int = 0  # ... by a native @Query
    inherited: int = 0  # calls of the library's repository methods linked to a table
    entity_manager: int = 0  # EntityManager and Session calls linked to a table
    statements: int = 0  # native SQL (SqlStatement nodes)
    sql_errors: int = 0
    unresolved: int = 0  # repositories, queries and calls whose entity wasn't found
    migrations: int = 0  # tables the migrations leave created
    naming: str = "spring"
    mismatches: list[str] = field(default_factory=list[str])  # entity tables no migration makes


@dataclass(slots=True)
class JpaGraph:
    nodes: list[GraphNode]  # the data layer's: the nodes given to :func:`link` and its own
    edges: list[GraphEdge]  # its own
    report: JpaReport


@dataclass(slots=True)
class _Entity:
    declared: Declared
    name: str  # the JPA entity name
    table: str
    store: Literal["jpa", "mongo"]
    named: str  # how the table is named: explicit, entity name, class name, inherited
    schema: str | None = None

    @property
    def key(self) -> str:
        return self.declared.type.key

    @property
    def model(self) -> str:
        return f"orm_model {self.key}"

    @property
    def path(self) -> str:
        return self.declared.path


def _names(annotations: Iterable[Annotation]) -> dict[str, Annotation]:
    return {a.name.rsplit(".", 1)[-1]: a for a in annotations}


class _Linker:
    def __init__(self, files: Sequence[FileJpa], known: Sequence[GraphNode]) -> None:
        self.files = files
        self.facts = {f.path: f.java for f in files if f.java is not None}
        self.index = TypeIndex(self.facts)
        self._resolver: CallResolver | None = None
        self.transient = frozenset(k for f in files for k in f.transient)
        self.nodes: dict[str, GraphNode] = {n.key: n for n in known}
        self.edges: list[GraphEdge] = []
        self.report = JpaReport()
        configured = next(
            (f.naming for f in sorted(files, key=lambda f: _config_rank(f.path)) if f.naming), None
        )
        last = configured.rsplit(".", 1)[-1] if configured else None
        self.naming = NAMINGS.get(last or "", "spring")
        self.report.naming = (
            self.naming
            if configured is None or last in NAMINGS
            else (f"spring (unknown strategy {last})")
        )
        self.entities: dict[str, _Entity] = {}
        self.by_name: dict[str, list[_Entity]] = {}
        self.repositories: dict[str, _Entity] = {}
        self.ids: dict[str, str] = {}  # each entity's id column, once read

    @property
    def resolver(self) -> CallResolver:
        if self._resolver is None:
            self._resolver = CallResolver(self.index)
        return self._resolver

    # names

    def fqn(self, declared: Declared, annotation: Annotation) -> str | None:
        """What an annotation's name means where it's written: its FQN, ``jakarta.persistence``'s
        when only an on-demand import of a persistence package can give it."""
        ref = self.index.resolve(declared.path, declared.type.key, annotation.name)
        if ref is not None:
            return ref.fqn
        for item in declared.facts.imports:
            if item.kind == "wildcard" and f"{item.source}.".startswith(PERSISTENCE):
                return f"{item.source}.{annotation.name}"
        return None

    def persistence(self, declared: Declared, annotations: Iterable[Annotation]) -> set[str]:
        """The simple names of the JPA annotations among ``annotations``."""
        return {
            a.name.rsplit(".", 1)[-1]
            for a in annotations
            if (fqn := self.fqn(declared, a)) is not None and fqn.startswith(PERSISTENCE)
        }

    # nodes

    def node(self, node: GraphNode) -> str:
        self.nodes.setdefault(node.key, node)
        return node.key

    def table(self, name: str, *, path: str | None, line: int, source: str, **attrs: Any) -> str:
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
                attrs={"sources": [source], **attrs},
                owner=path,
            )
            self.report.tables += 1
        elif source not in existing.attrs.get("sources", []):
            sources = [*existing.attrs.get("sources", []), source]
            self.nodes[key] = _with_attrs(existing, sources=sources, **attrs)
        return key

    def column(
        self, table_key: str, table: str, name: str, *, path: str, line: int, **attrs: Any
    ) -> str:
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
                attrs={"table": table_key, **attrs},
                owner=path,
            )
            self.report.columns += 1
        return key

    def maps(self, src: str, dst: str, path: str, line: int, **attrs: Any) -> None:
        self.edges.append(
            GraphEdge(
                src,
                dst,
                EdgeType.MAPS_TO,
                Provenance.CONTRACT,
                owner=path,
                evidence_path=path,
                evidence_line=line,
                attrs=attrs,
            )
        )

    def access(
        self,
        src: str,
        table_key: str,
        *,
        write: bool,
        how: Provenance,
        path: str,
        line: int,
        confidence: float | None = None,
        **attrs: Any,
    ) -> None:
        self.edges.append(
            GraphEdge(
                src,
                table_key,
                EdgeType.WRITES if write else EdgeType.READS,
                how,
                confidence=confidence if confidence is not None else confidence_of(how),
                evidence_path=path,
                evidence_line=line,
                attrs=attrs,
                owner=path,
            )
        )

    # passes

    def run(self) -> JpaGraph:
        if self.facts:
            self.find_entities()
            for entity in sorted(self.entities.values(), key=lambda e: e.key):
                self.model(entity)
            self.find_repositories()
        self.migrations()
        if self.facts:
            for key, entity in sorted(self.repositories.items()):
                self.repository_methods(key, entity)
            self.calls()
        return JpaGraph(list(self.nodes.values()), self.edges, self.report)

    # entities

    def find_entities(self) -> None:
        for declared in sorted(self.index.by_key.values(), key=lambda d: d.type.key):
            kind = declared.type
            if kind.kind != "class" or kind.local:
                continue
            marks = _names(kind.annotations)
            entity = marks.get("Entity")
            document = marks.get("Document")
            if entity is not None and "Entity" in self.persistence(declared, [entity]):
                name = string_expression(elements(entity.arguments).get("name", "")) or kind.name
                self.add(_Entity(declared, name, "", "jpa", ""))
            elif document is not None and self.fqn(declared, document) == MONGO_DOCUMENT:
                collection = _string(document.arguments, "collection", "value")
                table = collection or kind.name[:1].lower() + kind.name[1:]
                named = "explicit" if collection else "class name"
                self.add(_Entity(declared, kind.name, table, "mongo", named))
                self.report.documents += 1
        for entity in self.entities.values():
            if entity.store == "jpa":
                self.name_table(entity)

    def add(self, entity: _Entity) -> None:
        self.entities[entity.key] = entity
        self.by_name.setdefault(entity.name, []).append(entity)

    def entity_parent(self, entity: _Entity) -> _Entity | None:
        parent = self.index.superclass(entity.key)
        seen = 0
        while parent is not None and not parent.external and seen < MAX_DEPTH:
            found = self.entities.get(parent.key)
            if found is not None:
                return found
            parent = self.index.superclass(parent.key)
            seen += 1
        return None

    def strategy(self, entity: _Entity) -> str:
        """The inheritance strategy of an entity's hierarchy (its root's ``@Inheritance``)."""
        root = entity
        for _ in range(MAX_DEPTH):
            parent = self.entity_parent(root)
            if parent is None:
                break
            root = parent
        inheritance = _names(root.declared.type.annotations).get("Inheritance")
        text = (inheritance.arguments or "") if inheritance is not None else ""
        for strategy in ("JOINED", "TABLE_PER_CLASS"):
            if strategy in text:
                return strategy
        return "SINGLE_TABLE"

    def name_table(self, entity: _Entity, depth: int = 0) -> None:
        if entity.table:
            return
        marks = _names(entity.declared.type.annotations)
        parent = self.entity_parent(entity) if depth < MAX_DEPTH else None
        if parent is not None and self.strategy(entity) == "SINGLE_TABLE":
            self.name_table(parent, depth + 1)
            entity.table, entity.schema, entity.named = parent.table, parent.schema, "inherited"
            return
        table = marks.get("Table")
        explicit = _string(table.arguments, "name", "value") if table is not None else None
        entity.schema = _string(table.arguments, "schema") if table is not None else None
        if explicit:
            entity.table, entity.named = physical(explicit, self.naming), "explicit"
        else:
            entity.table = physical(entity.name, self.naming)
            entity.named = (
                "entity name" if entity.name != entity.declared.type.name else ("class name")
            )

    def model(self, entity: _Entity) -> None:
        kind = entity.declared.type
        path = entity.path
        framework = entity.store
        attrs: dict[str, Any] = {
            "framework": framework,
            "table": entity.table,
            "symbol": entity.key,
            "entity": entity.name,
        }
        self.node(
            GraphNode(
                key=entity.model,
                kind=NodeKind.ORM_MODEL,
                name=kind.name,
                path=path,
                start_line=kind.line,
                end_line=kind.end_line,
                lang="java",
                attrs=attrs,
                owner=path,
            )
        )
        if entity.store == "jpa":
            self.report.entities += 1
        extra: dict[str, Any] = {"store": "mongo"} if entity.store == "mongo" else {}
        if entity.schema:
            extra["schema"] = entity.schema
        table_key = self.table(entity.table, path=path, line=kind.line, source="entity", **extra)
        self.maps(entity.key, entity.model, path, kind.line)
        self.maps(entity.model, table_key, path, kind.line, named=entity.named)
        for variable, column, owner, attrs in self.columns_of(entity):
            column_key = self.column(
                table_key, entity.table, column, path=owner.path, line=variable.line, **attrs
            )
            if variable.key is not None:
                self.maps(variable.key, column_key, owner.path, variable.line)
        for name, line, columns in self.join_tables(entity):
            key = self.table(name, path=path, line=line, source="entity", join=True)
            for column in columns:
                self.column(key, name, column, path=path, line=line)

    def columns_of(self, entity: _Entity) -> list[tuple[Variable, str, Declared, dict[str, Any]]]:
        """The entity's columns: (field, column, where the field is, the column's attrs); its
        own fields, then its mapped superclasses' (and, when its table is its own under
        ``TABLE_PER_CLASS`` or the root's under ``SINGLE_TABLE``, the parent entities')."""
        found: list[tuple[Variable, str, Declared, dict[str, Any]]] = []
        seen: set[str] = set()
        strategy = self.strategy(entity)
        current = self.index.by_key.get(entity.key)
        depth = 0
        while current is not None and depth < MAX_DEPTH:
            depth += 1
            found += self.fields_of(entity, current, seen)
            parent = self.index.superclass(current.type.key)
            current = (
                None if parent is None or parent.external else self.index.by_key.get(parent.key)
            )
            if current is None:
                break
            marks = _names(current.type.annotations)
            if "MappedSuperclass" in marks:
                continue
            if current.type.key in self.entities and strategy == "TABLE_PER_CLASS":
                continue
            break  # a plain class (unmapped) or a parent entity with its own table
        return found

    def fields_of(
        self, entity: _Entity, declared: Declared, seen: set[str], depth: int = 0
    ) -> list[tuple[Variable, str, Declared, dict[str, Any]]]:
        found: list[tuple[Variable, str, Declared, dict[str, Any]]] = []
        for variable in declared.type.fields:
            if variable.static or variable.key in self.transient or variable.name in seen:
                continue
            marks = _names(variable.annotations)
            if marks.keys() & _NOT_COLUMNS:
                continue
            if entity.store == "mongo":
                seen.add(variable.name)
                written = (
                    _string(marks["Field"].arguments, "name", "value")
                    if ("Field" in marks)
                    else None
                )
                column = "_id" if "Id" in marks or variable.name == "id" else variable.name
                found.append((variable, written or column, declared, {}))
                continue
            if marks.keys() & _TO_MANY:
                continue
            relation = marks.keys() & _TO_ONE
            if relation:
                owning = all("mappedBy" not in elements(marks[r].arguments) for r in relation)
                if not owning:
                    continue
                seen.add(variable.name)
                target = self.target(declared, variable)
                join = marks.get("JoinColumn")
                name = _string(join.arguments, "name", "value") if join is not None else None
                column = (
                    physical(name, self.naming)
                    if name
                    else physical(f"{variable.name}_{self.id_column(target)}", self.naming)
                )
                attrs = {"references": f"table {target.table.lower()}"} if target else {}
                found.append((variable, column, declared, attrs))
                continue
            embedded = self.embeddable(declared, variable)
            if (marks.keys() & _EMBEDDED or embedded is not None) and depth < MAX_DEPTH:
                if embedded is not None:
                    seen.add(variable.name)
                    found += self.fields_of(entity, embedded, set(), depth + 1)
                continue
            seen.add(variable.name)
            column = marks.get("Column")
            name = _string(column.arguments, "name") if column is not None else None
            found.append((variable, physical(name or variable.name, self.naming), declared, {}))
        return found

    def target(self, declared: Declared, variable: Variable) -> _Entity | None:
        ref = self.index.resolve(declared.path, declared.type.key, variable.type or "")
        return self.entities.get(ref.key) if ref is not None else None

    def embeddable(self, declared: Declared, variable: Variable) -> Declared | None:
        ref = self.index.resolve(declared.path, declared.type.key, variable.type or "")
        held = self.index.by_key.get(ref.key) if ref is not None and not ref.external else None
        if held is not None and "Embeddable" in _names(held.type.annotations):
            return held
        return None

    def id_column(self, entity: _Entity | None) -> str:
        """The id column of an entity (``id`` when it can't be told): its ``@Id`` field's, its
        own or a mapped superclass's, read without following its relations (an entity may refer
        to itself, an employee to their manager, or round a cycle), once per entity."""
        if entity is None:
            return "id"
        held = self.ids.get(entity.key)
        if held is None:
            held = self.ids[entity.key] = self.read_id(entity)
        return held

    def read_id(self, entity: _Entity) -> str:
        current = self.index.by_key.get(entity.key)
        for _ in range(MAX_DEPTH):
            if current is None:
                break
            for variable in current.type.fields:
                marks = _names(variable.annotations)
                if variable.static or "Id" not in marks:
                    continue
                if entity.store == "mongo":
                    return "_id"
                column = marks.get("Column")
                name = _string(column.arguments, "name") if column is not None else None
                return physical(name or variable.name, self.naming)
            parent = self.index.superclass(current.type.key)
            current = (
                None if parent is None or parent.external else self.index.by_key.get(parent.key)
            )
        return "id"

    def join_tables(self, entity: _Entity) -> Iterator[tuple[str, int, list[str]]]:
        """The ``@JoinTable(name = ...)`` tables an entity's relationships declare."""
        for variable in entity.declared.type.fields:
            marks = _names(variable.annotations)
            join = marks.get("JoinTable")
            name = _string(join.arguments, "name", "value") if join is not None else None
            if join is None or not name:
                continue
            columns = [physical(c, self.naming) for c in _join_columns(join.arguments)]
            yield physical(name, self.naming), join.line, columns

    # repositories

    def find_repositories(self) -> None:
        for declared in sorted(self.index.by_key.values(), key=lambda d: d.type.key):
            if declared.type.kind != "interface":
                continue
            domain = self.domain(declared.type.key, 0)
            if not isinstance(domain, TypeRef):
                continue
            entity = self.entities.get(domain.key)
            if entity is None:
                self.report.unresolved += 1  # a repository of a type we don't map
                continue
            self.repositories[declared.type.key] = entity
            self.report.repositories += 1
            table_key = f"table {entity.table.lower()}"
            self.edges.append(
                GraphEdge(
                    declared.type.key,
                    table_key,
                    EdgeType.MAPS_TO,
                    Provenance.IMPORT_RESOLVED,
                    evidence_path=declared.path,
                    evidence_line=declared.type.line,
                    attrs={"via": "repository", "model": entity.model},
                    owner=declared.path,
                )
            )

    def domain(self, key: str, depth: int) -> TypeRef | int | None:
        """A repository's entity: the type, or (an interface generic in it) the position of its
        type parameter that is (``BaseRepository<T, ID> extends JpaRepository<T, ID>``: 0)."""
        declared = self.index.by_key.get(key)
        if declared is None or depth > MAX_DEPTH:
            return None
        for _, ref, written in self.index.supertypes(key):
            if ref is None:
                continue
            arguments = type_arguments(written)
            if ref.external:
                spring = ref.fqn.startswith("org.springframework.data")
                if not spring or ref.fqn.rsplit(".", 1)[-1] not in REPOSITORIES or not arguments:
                    continue
                first = arguments[0]
            else:
                inner = self.domain(ref.key, depth + 1)
                if inner is None:
                    continue
                if isinstance(inner, TypeRef):
                    return inner
                if inner >= len(arguments):
                    continue
                first = arguments[inner]
            name = base_name(first)
            parameters = declared.type.type_parameters
            if name in parameters:
                return parameters.index(name)
            found = self.index.resolve(declared.path, declared.type.outer or key, first)
            if found is not None:
                return found
        return None

    def repository_methods(self, key: str, entity: _Entity) -> None:
        declared = self.index.by_key[key]
        table_key = f"table {entity.table.lower()}"
        for method in declared.type.methods:
            if method.constructor or method.static:
                continue
            marks = _names(method.annotations)
            query = marks.get("Query")
            path, line = declared.path, method.line
            if query is not None:
                self.query(
                    method.key,
                    entity=entity,
                    query=query,
                    modifying="Modifying" in marks,
                    path=path,
                    line=line,
                )
                continue
            derived = _DERIVED_RE.match(method.name)
            if method.name in INHERITED_WRITES or method.name in INHERITED_READS:
                write = method.name in INHERITED_WRITES
                via = f"repository.{method.name}"
            elif derived is not None:
                write = derived[1] in _DERIVED_WRITES
                via = "derived"
                self.report.derived += 1
            else:
                continue  # a custom fragment's, a default method: its code says what it does
            self.access(
                method.key,
                table_key,
                write=write,
                how=Provenance.IMPORT_RESOLVED,
                path=path,
                line=line,
                via=via,
                model=entity.model,
            )
            self.report.methods += 1

    def query(
        self,
        src: str,
        *,
        entity: _Entity,
        query: Annotation,
        modifying: bool,
        path: str,
        line: int,
    ) -> None:
        found = elements(query.arguments)
        text = string_expression(found.get("value", "")) if "value" in found else None
        table_key = f"table {entity.table.lower()}"
        if entity.store == "mongo":  # a JSON filter on the repository's collection
            write = found.get("delete", "").strip() == "true"
            self.access(
                src,
                table_key,
                write=write,
                how=Provenance.CONTRACT,
                path=path,
                line=line,
                via="@Query",
                model=entity.model,
            )
            self.report.methods += 1
            return
        if not text:
            self.report.unresolved += 1
            return
        text = text.replace("#{#entityName}", entity.name)
        if found.get("nativeQuery", "").strip() == "true":
            linked = self.statement(src, text, path, line, "@Query", how=Provenance.CONTRACT)
            self.report.native += 1 if linked else 0
        else:
            linked = self.jpql(
                src,
                text,
                modifying=modifying,
                path=path,
                line=line,
                via="@Query",
                how=Provenance.CONTRACT,
            )
            self.report.queries += 1 if linked else 0
        if linked:
            self.report.methods += 1
        else:
            self.report.unresolved += 1

    def jpql(
        self,
        src: str,
        text: str,
        *,
        modifying: bool,
        path: str,
        line: int,
        via: str,
        how: Provenance,
    ) -> bool:
        """``src``'s edges to what a JPQL query names (False: it names no entity we map)."""
        parsed = jpql(text)
        linked = False
        writes = parsed.kind in ("update", "delete", "insert") or (modifying and not parsed.kind)
        named = [(parsed.target, writes)] if parsed.target else []
        named += [(n, False) for n in parsed.names]
        for name, write in named:
            entity = self.entity_named(name)
            if entity is None:
                continue
            self.access(
                src,
                f"table {entity.table.lower()}",
                write=write,
                how=how,
                path=path,
                line=line,
                via=via,
                model=entity.model,
                query="jpql",
            )
            linked = True
        return linked

    def entity_named(self, name: str) -> _Entity | None:
        """The entity a JPQL name means: its entity name, or its class's FQN."""
        found = self.by_name.get(name, [])
        if len(found) == 1:
            return found[0]
        declared = self.index.by_fqn.get(name) if "." in name else None
        return self.entities.get(declared.type.key) if declared is not None else None

    def statement(
        self,
        src: str,
        sql: str,
        path: str,
        line: int,
        via: str,
        *,
        how: Provenance,
        column: int = 0,
    ) -> bool:
        """Native SQL: a ``SqlStatement`` node and its edges, and ``src``'s (as orm.py's raw
        SQL; ``how`` for the tables something declares)."""
        sql = re.sub(r"\?\d+", "?", re.sub(r":#\{[^}]*\}", "?", sql))
        if not looks_like_sql(sql):
            return False
        parsed = tables(sql)
        key = f"sql_statement {path}:{line}"
        held = self.nodes.get(key)
        if held is not None and held.attrs.get("sql") != sql[:MAX_SQL_ATTR]:
            key = f"{key}:{column}"  # another statement on the line: each its own node
        key = self.node(
            GraphNode(
                key=key,
                kind=NodeKind.SQL_STATEMENT,
                name=" ".join(sql.split())[:MAX_NAME],
                path=path,
                start_line=line,
                end_line=line,
                lang="sql",
                attrs={
                    "kind": parsed.kind,
                    "sql": sql[:MAX_SQL_ATTR],
                    "via": via,
                    "function": src,
                    **({"error": parsed.error} if parsed.error else {}),
                },
                owner=path,
            )
        )
        self.report.statements += 1
        if parsed.error:
            self.report.sql_errors += 1
        linked = False
        for names, write in ((parsed.reads, False), (parsed.writes, True)):
            for name in names:
                table_key, known = self.sql_table(name, line)
                provenance = how if known else Provenance.NAME_UNIQUE
                confidence = confidence_of(provenance)
                if known and how is Provenance.NAME_UNIQUE:
                    confidence = KNOWN_TABLE_CONFIDENCE
                for source in (key, src):
                    self.access(
                        source,
                        table_key,
                        write=write,
                        how=provenance,
                        path=path,
                        line=line,
                        confidence=confidence,
                        sql=key,
                        via=via,
                    )
                linked = True
        return linked

    def sql_table(self, name: str, line: int) -> tuple[str, bool]:
        """The table node SQL's ``name`` means, and whether something declares it."""
        for candidate in (name, name.rsplit(".", 1)[-1]):
            node = self.nodes.get(f"table {candidate.lower()}")
            if node is not None:
                declared = any(s in node.attrs.get("sources", []) for s in _DECLARED)
                return self.table(node.name, path=node.path, line=line, source="sql"), declared
        return self.table(name, path=None, line=0, source="sql"), False

    # calls

    def calls(self) -> None:
        """Calls of the library's repository methods, and of an ``EntityManager``'s and a
        ``JdbcTemplate``'s."""
        inherited = INHERITED_WRITES | INHERITED_READS
        for path in sorted(self.facts):
            facts = self.facts[path]
            if self.repositories:
                for position, call in enumerate(facts.calls):
                    if call.kind == "method" and call.name in inherited:
                        self.inherited_call(path, position, call)
        for file in self.files:
            facts = file.java
            if facts is None or not file.arguments:
                continue
            calls = {(c.line, c.column, c.name): c for c in facts.calls if c.kind == "method"}
            for argument in file.arguments:
                call = calls.get((argument.line, argument.column, argument.method))
                if call is not None:
                    self.argument_call(file.path, call, argument)

    def inherited_call(self, path: str, position: int, call: Call) -> None:
        resolution = self.resolver.resolve(path, position)
        for hit in resolution.hits:
            receiver = hit.attrs.get("receiver")
            entity = self.repositories.get(receiver) if isinstance(receiver, str) else None
            if entity is None or not hit.key.startswith("external_symbol "):
                continue
            self.access(
                call.source,
                f"table {entity.table.lower()}",
                write=call.name in INHERITED_WRITES,
                how=Provenance.RECEIVER_HEURISTIC,
                path=path,
                line=call.line,
                via=f"repository.{call.name}",
                model=entity.model,
                repository=receiver,
            )
            self.report.inherited += 1
            return

    def argument_call(self, path: str, call: Call, argument: Argument) -> None:
        value = self.resolver.value(path, call)
        ref = value.ref if value is not None and value.kind == "instance" else None
        fqn = ref.fqn if ref is not None and ref.external else ""
        name = call.name
        if fqn in _JDBC and name in _JDBC_SQL and argument.kind == "string":
            if self.statement(
                call.source,
                argument.value,
                path,
                call.line,
                "jdbc",
                how=Provenance.NAME_UNIQUE,
                column=call.column,
            ):
                self.report.entity_manager += 1
            return
        if fqn not in _MANAGERS:
            return
        via = f"entity_manager.{name}"
        if name in _MANAGER_JPQL and argument.kind == "string":
            how = Provenance.IMPORT_RESOLVED
            if self.jpql(
                call.source,
                argument.value,
                modifying=False,
                path=path,
                line=call.line,
                via=via,
                how=how,
            ):
                self.report.entity_manager += 1
            return
        if name in _MANAGER_SQL and argument.kind == "string":
            if self.statement(
                call.source,
                argument.value,
                path,
                call.line,
                via,
                how=Provenance.NAME_UNIQUE,
                column=call.column,
            ):
                self.report.entity_manager += 1
            return
        write = name in _MANAGER_WRITES
        if not write and name not in _MANAGER_READS:
            return
        entity, how = self.argument_entity(path, call, argument)
        if entity is None:
            self.report.unresolved += 1
            return
        self.access(
            call.source,
            f"table {entity.table.lower()}",
            write=write,
            how=how,
            path=path,
            line=call.line,
            via=via,
            model=entity.model,
        )
        self.report.entity_manager += 1

    def argument_entity(
        self, path: str, call: Call, argument: Argument
    ) -> tuple[_Entity | None, Provenance]:
        """The entity a call's argument is: a class literal's or ``new``'s type
        (``import_resolved``), or a variable's declared type (``receiver_heuristic``)."""
        if argument.kind in ("class", "new"):
            ref = self.index.resolve(path, call.owner, argument.value)
            entity = self.entities.get(ref.key) if ref is not None else None
            return entity, Provenance.IMPORT_RESOLVED
        if argument.kind != "name":
            return None, Provenance.RECEIVER_HEURISTIC
        probe = replace(call, receiver=Receiver("name", tuple(argument.value.split("."))))
        value = self.resolver.value(path, probe)
        ref = value.ref if value is not None and value.kind == "instance" else None
        entity = self.entities.get(ref.key) if ref is not None else None
        return entity, Provenance.RECEIVER_HEURISTIC

    # migrations

    def created(self) -> dict[str, tuple[str, TableChange, str]]:
        """The tables the migrations leave created, by lower-cased name (in order: Flyway's by
        version, the rest by path): where, the change that made it, the tool."""
        created: dict[str, tuple[str, TableChange, str]] = {}
        ordered = sorted(
            (f for f in self.files if f.migrations), key=lambda f: (_version(f.path), f.path)
        )
        for f in ordered:
            for change in f.migrations:
                name = change.table.rsplit(".", 1)[-1].lower()
                if change.op == "create_table":
                    created[name] = (f.path, change, f.tool or "")
                elif change.op == "rename_table" and change.renamed_to:
                    held = created.pop(name, None)
                    new = change.renamed_to.rsplit(".", 1)[-1].lower()
                    created[new] = (
                        (f.path, change, f.tool or "")
                        if held is None
                        else (held[0], replace(held[1], table=change.renamed_to), held[2])
                    )
                else:
                    created.pop(name, None)
        return created

    def migrations(self) -> None:
        """The migrations' tables, checked against the entities' (see the module)."""
        created = self.created()
        if not created:
            return
        self.report.migrations = len(created)
        for key, node in list(self.nodes.items()):
            if node.kind is not NodeKind.TABLE or "entity" not in node.attrs.get("sources", []):
                continue
            if node.attrs.get("store") == "mongo":
                continue
            known = node.name.lower() in created
            self.nodes[key] = _with_attrs(node, in_migrations=known)
            if not known:
                self.report.mismatches.append(node.name)
        for path, change, tool in created.values():
            name = (change.renamed_to or change.table).rsplit(".", 1)[-1]
            key = self.table(name, path=path, line=change.line, source="migration")
            self.nodes[key] = _with_attrs(self.nodes[key], in_migrations=True, migration=tool)
            if self.nodes[key].owner == path:
                for column, line in change.columns:
                    self.column(key, name, column, path=path, line=line)


def _version(path: str) -> tuple[int, ...]:
    """A Flyway migration's version (``V1_2__x.sql``: (1, 2); others sort first)."""
    found = re.match(r"^V(\d[\d._]*)__", basename(path))
    if found is None:
        return ()
    return tuple(int(p) for p in re.split(r"[._]", found[1]) if p.isdigit())


def _with_attrs(node: GraphNode, **attrs: Any) -> GraphNode:
    return replace(node, attrs={**node.attrs, **attrs})


def link(files: Iterable[FileJpa], known: Sequence[GraphNode] = ()) -> JpaGraph:
    """The snapshot's JPA graph, from every file's :func:`scan` and :func:`scan_text` (``known``:
    the data layer's nodes so far, orm.py's: a table both name is one node)."""
    return _Linker(list(files), known).run()
