"""A schema's entity-relationship diagram (6.6): its tables, their columns and the foreign keys
between them, as the code declares them.

The links stage's tables and columns are read (:mod:`sensei_engine.link.orm` for SQLModel,
SQLAlchemy and Alembic, :mod:`sensei_engine.link.jpa` for JPA, Flyway and Liquibase): each
column with its type, whether it is the key and whether it may be null, and the column a
foreign key names. Each table is an entity listing its columns (its key first), each foreign
key a relation from the table holding it to the table it names: many to one, or one to one when
the key is the table's own. Nothing in it is a model's, so it costs nothing, draws the same way
every time, and every line is verified: the code declares it.

The whole schema is drawn, or (for a question) the tables it reaches and the ones their foreign
keys name, :data:`AROUND` hops out; past :data:`MAX_TABLES`, the most related.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sensei_core.models import EdgeType
from sensei_engine.diagrams import DiagramSpec, Member, SpecEdge, SpecNode
from sensei_engine.graph.snapshot import GraphSnapshot

MAX_TABLES = 60
MAX_COLUMNS = 40
AROUND = 1  # foreign-key hops out from a question's tables
DECLARED = frozenset({"model", "migration", "table", "entity"})  # a table the code declares
REACH = frozenset({EdgeType.READS, EdgeType.WRITES, EdgeType.MAPS_TO})


@dataclass(frozen=True, slots=True)
class SchemaNode:
    """A table, column or model node of the graph, with its attrs."""

    key: str
    kind: str  # table, column, orm_model
    name: str
    path: str | None
    start_line: int
    end_line: int
    attrs: Mapping[str, Any] = field(default_factory=dict[str, Any])


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _member(column: SchemaNode, tables: Mapping[str, SchemaNode]) -> Member:
    attrs = column.attrs
    target = _text(attrs.get("references"))
    ref = None
    if target is not None:
        table = tables.get(target)
        name = table.name if table is not None else target.removeprefix("table ")
        ref = f"{name}.{_text(attrs.get('references_column')) or 'id'}"
    nullable = attrs.get("nullable")
    return Member(
        column.name,
        "pk" if attrs.get("pk") is True else ("fk" if ref else "column"),
        _text(attrs.get("type")),
        ref,
        nullable if isinstance(nullable, bool) else None,
    )


def schema(
    rows: Iterable[SchemaNode], title: str, *, only: Collection[str] | None = None
) -> DiagramSpec:
    """The ER diagram of the tables in ``rows`` (see the module): all of them, or the tables
    ``only`` names (their keys) and those their foreign keys reach, :data:`AROUND` hops out."""
    tables: dict[str, SchemaNode] = {}
    columns: defaultdict[str, list[SchemaNode]] = defaultdict(list)
    models: defaultdict[str, list[SchemaNode]] = defaultdict(list)
    for row in rows:
        if row.kind == "table":
            tables[row.key] = row
        elif row.kind == "column" and (table := _text(row.attrs.get("table"))):
            columns[table].append(row)
        elif row.kind == "orm_model" and (table := _text(row.attrs.get("table"))):
            models[f"table {table.lower()}"].append(row)
    drawn = {
        key
        for key, table in tables.items()
        if columns.get(key) or DECLARED & set(table.attrs.get("sources") or ())
    }
    links: defaultdict[str, set[str]] = defaultdict(set)  # both ways, for the neighbourhood
    for key in drawn:
        for column in columns.get(key, []):
            target = _text(column.attrs.get("references"))
            if target in drawn and target != key:
                links[key].add(target)
                links[target].add(key)
    if only is not None:
        kept = {key for key in only if key in drawn}
        frontier = set(kept)
        for _ in range(AROUND):
            frontier = {n for key in frontier for n in links[key]} - kept
            kept |= frontier
        drawn = kept
    chosen = sorted(drawn, key=lambda key: (-len(links[key] & drawn), tables[key].name.lower()))
    chosen = sorted(chosen[:MAX_TABLES], key=lambda key: tables[key].name.lower())
    kept_set = set(chosen)

    nodes: list[SpecNode] = []
    edges: list[SpecEdge] = []
    for key in chosen:
        table = tables[key]
        held = sorted(
            columns.get(key, []),
            key=lambda c: (c.attrs.get("pk") is not True, c.start_line, c.name),
        )
        members = tuple(_member(c, tables) for c in held[:MAX_COLUMNS])
        cited = next(iter(sorted(models.get(key, []), key=lambda m: m.key)), table)
        kind = "collection" if table.attrs.get("store") == "mongo" else "table"
        nodes.append(
            SpecNode(
                key, table.name, "db", kind, cited.path, cited.start_line, cited.end_line,
                members, max(0, len(held) - MAX_COLUMNS),
            )
        )  # fmt: skip
        for column in held:
            target = _text(column.attrs.get("references"))
            if target is None or target not in kept_set:
                continue
            refcol = _text(column.attrs.get("references_column")) or "id"
            relation = "one_to_one" if column.attrs.get("pk") is True else "many_to_one"
            edges.append(
                SpecEdge(
                    key, target, relation, f"{column.name} → {refcol}", True, 1.0, "contract",
                    None,
                )
            )  # fmt: skip
    return DiagramSpec(title, "er", nodes, edges)


def reached_tables(graph: GraphSnapshot, keys: Iterable[str]) -> set[str]:
    """The tables among ``keys`` and the ones their code reads, writes or maps to (a class
    through its model)."""
    found: set[str] = set()
    for key in keys:
        if key.startswith("table "):
            found.add(key)
        for dst, edge in graph.edges_out(key):
            if edge.type not in REACH:
                continue
            if dst.startswith("table "):
                found.add(dst)
            elif dst.startswith("orm_model "):
                found |= {t for t, _ in graph.edges_out(dst) if t.startswith("table ")}
    return found
