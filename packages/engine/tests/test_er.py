"""The schema's ER diagram: the fastapi-template's tables, their columns and foreign key, from
what the ORM linker reads of its models; and a question's tables with their neighbours."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from sensei_engine.diagrams import DiagramSpec, mermaid
from sensei_engine.er import SchemaNode, schema
from sensei_engine.link.orm import FileOrm, link, scan
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"


@pytest.fixture(scope="module")
def rows(tmp_path_factory: pytest.TempPathFactory) -> list[SchemaNode]:
    root = tmp_path_factory.mktemp("fixture")
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(root, filter="data")
    repo = root / "fastapi-template"
    scanned: list[FileOrm] = []
    for path in sorted(repo.rglob("*.py")):
        result = parse_file(str(path.relative_to(repo)), path.read_bytes())
        scanned.append(scan(result, extract_symbols(result, "python")))
    graph = link(scanned)
    return [
        SchemaNode(n.key, n.kind.value, n.name, n.path, n.start_line, n.end_line, n.attrs)
        for n in graph.nodes
    ]


def test_the_whole_schema(rows: list[SchemaNode]) -> None:
    spec = schema(rows, "Schema")
    assert [n.label for n in spec.nodes] == ["item", "user"]
    item = next(n for n in spec.nodes if n.label == "item")
    assert item.members[0].name == "id"
    assert item.members[0].kind == "pk"
    owner = next(m for m in item.members if m.name == "owner_id")
    assert (owner.kind, owner.type, owner.ref, owner.nullable) == ("fk", "UUID", "user.id", False)
    assert item.path == "backend/app/models.py"  # cited at the model, not the migration
    [edge] = spec.edges
    assert (edge.src, edge.dst, edge.type, edge.label) == (
        "table item", "table user", "many_to_one", "owner_id → id",
    )  # fmt: skip
    assert edge.verified
    assert spec.verified_ratio == 1
    drawn = mermaid(spec)
    assert drawn.startswith("erDiagram\n")
    assert "    UUID owner_id FK\n" in drawn
    assert "    UUID id PK\n" in drawn
    assert '  item }o--|| user : "owner_id → id"\n' in drawn
    # Stored and read back the same.
    assert DiagramSpec.from_json(spec.as_json()) == spec


def test_a_questions_tables_and_their_neighbours(rows: list[SchemaNode]) -> None:
    spec = schema(rows, "Items", only={"table item"})
    assert {n.label for n in spec.nodes} == {"item", "user"}  # the key's table comes along
    assert schema(rows, "None", only=set()).nodes == []
