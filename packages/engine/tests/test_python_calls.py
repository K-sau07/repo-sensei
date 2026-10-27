"""Python calls, references and bases (3.4): every confidence tier on a small project, Python's
scoping (a parameter shadows an import, a class body isn't seen from its methods), and
precision on 200+ hand-checked edges of the fastapi-template."""

from __future__ import annotations

import tarfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph import python as py
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.writer import CODE, GraphEdge
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

FIXTURES = Path(__file__).parent / "fixtures"

FILES = {
    "pyproject.toml": '[project]\nname = "demo"\n',
    "shop/__init__.py": "",
    "shop/db.py": (
        "from typing import Annotated\n"
        "from sqlmodel import Session\n"
        "\n"
        "\n"
        "def get_db():\n"
        "    pass\n"
        "\n"
        "\n"
        "SessionDep = Annotated[Session, get_db]\n"
    ),
    "shop/models.py": (
        "from sqlmodel import SQLModel\n"
        "\n"
        "\n"
        "class Base(SQLModel):\n"
        "    def save(self):\n"
        "        return self.validate()\n"
        "\n"
        "    def validate(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "class Item(Base):\n"
        "    title: str = ''\n"
        "\n"
        "    def save(self):\n"
        "        super().save()\n"
        "        return self.title\n"
        "\n"
        "    @classmethod\n"
        "    def make(cls):\n"
        "        return cls.build()\n"
        "\n"
        "    @staticmethod\n"
        "    def build():\n"
        "        return Item()\n"
    ),
    "shop/repo.py": (
        "from shop.models import Item\n"
        "\n"
        "\n"
        "class Repo:\n"
        "    def find(self) -> Item:\n"
        "        return Item()\n"
        "\n"
        "    def archive(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "class Cache:\n"
        "    def archive(self):\n"
        "        pass\n"
        "\n"
        "    def flush(self):\n"
        "        pass\n"
        "\n"
        "    def append(self, value):\n"
        "        pass\n"
    ),
    "shop/a.py": "class A:\n    def ping(self):\n        pass\n",
    "shop/b.py": "class B:\n    def ping(self):\n        pass\n",
    "shop/c.py": "class C:\n    def ping(self):\n        pass\n",
    "shop/d.py": "class D:\n    def ping(self):\n        pass\n",
    "shop/e.py": "class E:\n    def ping(self):\n        pass\n",
    "shop/f.py": "class F:\n    def ping(self):\n        pass\n",
    "shop/service.py": (
        "from shop import models\n"
        "from shop.db import SessionDep, get_db\n"
        "from shop.models import Item\n"
        "from shop.repo import Repo\n"
        "\n"
        "\n"
        "def audit(fn):\n"
        "    return fn\n"
        "\n"
        "\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self.repo = Repo()\n"
        "\n"
        "    @audit\n"
        "    def run(self, session: SessionDep, item: Item, other):\n"
        "        found = self.repo.find()\n"
        "        found.save()\n"
        "        item.save()\n"
        "        session.add(item)\n"
        "        other.flush()\n"
        "        other.archive()\n"
        "        other.ping()\n"
        "        other.append(1)\n"
        "        models.Item.make()\n"
        "        register(get_db)\n"
        "        return len(found.title)\n"
        "\n"
        "\n"
        "def register(callback):\n"
        "    def inner():\n"
        "        return callback()\n"
        "    return inner()\n"
        "\n"
        "\n"
        "def shadowed(Item):\n"
        "    return Item()\n"
    ),
}


def _resolve(files: Mapping[str, str]) -> tuple[list[GraphEdge], py.PyStats]:
    definitions: dict[str, list[Definition]] = {}
    facts: dict[str, py.PyFileFacts] = {}
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        parsed = parse_file(path, content.encode())
        assert parsed.language is not None
        found = py.extract(parsed)
        assert found is not None
        facts[path] = found
        definitions[path] = [
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, parsed.language)
        ]
    configs = {path: content for path, content in files.items() if is_config(path)}
    nodes, edges, stats = py.resolve(Project(frozenset(files), configs, definitions), facts)
    CODE.check(nodes, edges)  # the graph stage's layer
    assert {e.dst for e in edges if not e.dst.startswith("py ")} <= {n.key for n in nodes}
    return edges, stats


def _found(edges: list[GraphEdge], path: str) -> set[tuple[int, str, str, str, float]]:
    return {
        (
            e.evidence_line or 0,
            e.type.value,
            e.dst.removeprefix("py "),
            e.provenance.value,
            round(e.confidence or 0, 2),
        )
        for e in edges
        if e.evidence_path == path and e.type is not EdgeType.IMPORTS
    }


def test_every_tier_and_scope() -> None:
    edges, stats = _resolve(FILES)
    run = "py shop/service.py/Service#run()."
    assert {
        e.src
        for e in edges
        if e.evidence_path == "shop/service.py" and 15 <= (e.evidence_line or 0) <= 27
    } == {run}
    assert _found(edges, "shop/service.py") == {
        (13, "calls", "shop/repo.py/Repo#", "import_resolved", 0.9),  # self.repo = Repo()
        (15, "references", "shop/service.py/audit().", "import_resolved", 0.9),  # a decorator
        (16, "references", "shop/models.py/Item#", "import_resolved", 0.9),  # annotations
        (16, "references", "shop/db.py/SessionDep.", "import_resolved", 0.9),
        # self.repo: what __init__ assigned; found: Repo.find's return annotation
        (17, "calls", "shop/repo.py/Repo#find().", "receiver_heuristic", 0.7),
        (18, "calls", "shop/models.py/Item#save().", "receiver_heuristic", 0.7),
        (19, "calls", "shop/models.py/Item#save().", "receiver_heuristic", 0.7),  # item: Item
        # session: SessionDep, an alias of Annotated[Session, ...]: the external's method
        (20, "calls", "external_symbol py:sqlmodel/Session.add", "receiver_heuristic", 0.7),
        # other: unknown. flush: one method of the name; archive: two; ping: six (none)
        (21, "calls", "shop/repo.py/Cache#flush().", "name_unique", 0.4),
        (22, "calls", "shop/repo.py/Repo#archive().", "name_ambiguous", 0.1),
        (22, "calls", "shop/repo.py/Cache#archive().", "name_ambiguous", 0.1),
        # append: a builtin type's method, never guessed; models.Item.make: static
        (25, "calls", "shop/models.py/Item#make().", "import_resolved", 0.9),
        (26, "calls", "shop/service.py/register().", "import_resolved", 0.9),
        (26, "references", "shop/db.py/get_db().", "import_resolved", 0.9),  # a callback
        (27, "references", "shop/models.py/Item#title.", "receiver_heuristic", 0.7),  # len(): none
        (33, "calls", "shop/service.py/register().inner().", "import_resolved", 0.9),
        # callback() and shadowed's Item(): parameters, not definitions
    }
    assert _found(edges, "shop/models.py") == {
        (4, "inherits", "external_symbol py:sqlmodel/SQLModel", "import_resolved", 0.9),
        (6, "calls", "shop/models.py/Base#validate().", "receiver_heuristic", 0.7),
        (12, "inherits", "shop/models.py/Base#", "import_resolved", 0.9),
        (16, "calls", "shop/models.py/Base#save().", "receiver_heuristic", 0.7),  # super()
        (17, "references", "shop/models.py/Item#title.", "receiver_heuristic", 0.7),
        (21, "calls", "shop/models.py/Item#build().", "receiver_heuristic", 0.7),  # cls.
        (25, "calls", "shop/models.py/Item#", "import_resolved", 0.9),
    }
    assert set(stats.calls.by_provenance) == {
        Provenance.IMPORT_RESOLVED, Provenance.RECEIVER_HEURISTIC, Provenance.NAME_UNIQUE,
        Provenance.NAME_AMBIGUOUS,
    }  # fmt: skip
    assert (stats.calls.guessed, stats.calls.unresolved) == (2, 2)  # ping, append
    assert all(e.owner == e.evidence_path for e in edges)


def test_a_class_body_is_not_seen_from_its_methods() -> None:
    files = {
        "m.py": (
            "def helper():\n    pass\n\n\n"
            "class K:\n    helper = None\n\n    def run(self):\n        return helper()\n"
        )
    }
    edges, _ = _resolve(files)
    assert ("calls", "py m.py/helper().") in {(e.type.value, e.dst) for e in edges}


# ---------------------------------------------------------------------- fastapi-template


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("template")
    with tarfile.open(FIXTURES / "fastapi-template.tar.gz") as archive:
        archive.extractall(root, filter="data")
    base = root / "fastapi-template"
    return {
        str(path.relative_to(base)): path.read_text(errors="replace")
        for path in base.rglob("*")
        if path.is_file() and (path.suffix == ".py" or is_config(path.name))
    }


def _labels() -> set[tuple[str, int, str, str]]:
    labels: set[tuple[str, int, str, str]] = set()
    for row in (FIXTURES / "fastapi-template-calls.labels").read_text().splitlines():
        fact = row.split("    # ", 1)[0].split() if not row.startswith("#") else []  # keys hold #
        if fact:
            path, line = fact[0].rsplit(":", 1)
            labels.add((path, int(line), fact[1], " ".join(fact[2:])))
    return labels


def test_the_template_edges_are_precise(template: dict[str, str]) -> None:
    edges, stats = _resolve(template)
    labels = _labels()
    assert len(labels) >= 200
    lines = {(path, line, kind) for path, line, kind, _ in labels}
    found = {
        (e.evidence_path or "", e.evidence_line or 0, e.type.value, e.dst)
        for e in edges
        if (e.evidence_path, e.evidence_line, e.type.value) in lines
    }
    precision = len(found & labels) / len(found)
    recall = len(found & labels) / len(labels)
    assert precision >= 0.9, sorted(found - labels)
    assert recall >= 0.9, sorted(labels - found)
    assert stats.calls.resolved / stats.calls.calls >= 0.8  # the rest: what externals return
