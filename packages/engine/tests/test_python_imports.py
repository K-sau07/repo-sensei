"""Python import resolution (3.2): package roots, the module map, every import form, and the
resolution rate on the fastapi-template backend against hand labels."""

from __future__ import annotations

import tarfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.python.imports import (
    ModuleMap,
    PyFacts,
    extract,
    import_tables,
    package_roots,
    resolve,
)
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

ARCHIVE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"

FILES = {
    "pyproject.toml": '[project]\nname = "demo"\n',
    "pkg/__init__.py": 'from .core import Engine\n\n__all__ = ["Engine", "VERSION"]\nVERSION = 1\n',
    "pkg/core.py": "class Engine:\n    pass\n\n\ndef run():\n    pass\n\n\n_private = 1\n",
    "pkg/sub/__init__.py": "from ..core import run as go\n",
    "pkg/sub/helpers.py": "def tool():\n    pass\n",
    "pkg/sub/leaf.py": (
        "from __future__ import annotations\n"
        "import os\n"
        "import pkg.core\n"
        "import pkg.core as c\n"
        "from pkg import Engine\n"
        "from . import helpers\n"
        "from .helpers import tool\n"
        "from .. import core\n"
        "from pkg.sub import go\n"
        "from pkg.core import *\n"
        "from fastapi import FastAPI\n"
        "import numpy as np\n"
        "from .missing import x\n"
        "from pkg import *\n"
    ),
    "services/api/pyproject.toml": '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
    "services/api/src/svc/__init__.py": "",
    "services/api/src/svc/handlers.py": "def handle():\n    pass\n",
    "services/api/main.py": "from svc.handlers import handle\n",
}


def _project(files: Mapping[str, str]) -> tuple[Project, dict[str, PyFacts]]:
    definitions: dict[str, list[Definition]] = {}
    facts: dict[str, PyFacts] = {}
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        parsed = parse_file(path, content.encode())
        assert parsed.ok
        assert parsed.language is not None
        facts[path] = extract(parsed)
        definitions[path] = [
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, parsed.language)
        ]
    configs = {path: content for path, content in files.items() if is_config(path)}
    return Project(frozenset(files), configs, definitions), facts


def test_roots_and_names() -> None:
    roots = package_roots(FILES, {p: c for p, c in FILES.items() if is_config(p)})
    assert roots[-1] == ""  # the repository, last
    assert "services/api/src" in roots  # a configured "where"
    assert "services/api" in roots  # a pyproject's folder
    modules = ModuleMap.build(FILES)
    assert modules.path("pkg") == "pkg/__init__.py"
    assert modules.path("pkg.sub.leaf") == "pkg/sub/leaf.py"
    assert modules.path("svc.handlers") == "services/api/src/svc/handlers.py"
    assert modules.package_of("pkg/sub/leaf.py") == "pkg.sub"
    assert modules.package_of("pkg/sub/__init__.py") == "pkg.sub"


def test_every_import_form_resolves() -> None:
    project, facts = _project(FILES)
    leaf = import_tables(project, facts)["py pkg/sub/leaf.py/"]
    core, helpers = "py pkg/core.py/", "py pkg/sub/helpers.py/"
    assert leaf["os"] == "package py:os"  # import os: a package outside
    assert leaf["pkg"] == "py pkg/__init__.py/"  # import pkg.core binds pkg
    assert leaf["c"] == core  # import pkg.core as c
    assert leaf["Engine"] == "py pkg/core.py/Engine#"  # re-exported by pkg/__init__
    assert leaf["helpers"] == helpers  # from . import <submodule>
    assert leaf["tool"] == "py pkg/sub/helpers.py/tool()."
    assert leaf["core"] == core  # from .. import <submodule>
    assert leaf["go"] == "py pkg/core.py/run()."  # an alias re-exported two hops away
    assert leaf["run"] == "py pkg/core.py/run()."  # from pkg.core import *: public names
    assert "_private" not in leaf
    assert leaf["FastAPI"] == "external_symbol py:fastapi/FastAPI"
    assert leaf["np"] == "package py:numpy"
    assert "x" not in leaf  # .missing: no such module
    assert leaf["VERSION"] == "py pkg/__init__.py/VERSION."  # from pkg import *: its __all__
    main = import_tables(project, facts)["py services/api/main.py/"]
    assert main["handle"] == "py services/api/src/svc/handlers.py/handle()."


def test_imports_become_edges_and_externals_shared_nodes() -> None:
    project, facts = _project(FILES)
    nodes, edges, stats = resolve(project, facts)
    leaf = [e for e in edges if e.evidence_path == "pkg/sub/leaf.py"]
    assert {(e.dst, e.evidence_line) for e in leaf} >= {
        ("py pkg/core.py/", 3),
        ("py pkg/core.py/Engine#", 5),
        ("external_symbol py:fastapi/FastAPI", 11),
        ("package py:numpy", 12),
    }
    assert all(
        (e.src, e.type, e.provenance, e.owner) == (
            "py pkg/sub/leaf.py/", EdgeType.IMPORTS, Provenance.IMPORT_RESOLVED, "pkg/sub/leaf.py",
        )
        for e in leaf
    )  # fmt: skip
    externals = {n.key: n for n in nodes}
    assert externals["external_symbol py:fastapi/FastAPI"].kind is NodeKind.EXTERNAL_SYMBOL
    assert externals["package py:numpy"].kind is NodeKind.PACKAGE
    assert all(n.owner is None for n in nodes)  # shared: no one file's
    # leaf.py's .missing is the one internal import that doesn't resolve.
    assert [(p, line) for p, line, _ in stats.unresolved] == [("pkg/sub/leaf.py", 13)]


# ---------------------------------------------------------------------- fastapi-template

# Hand labels: imports of the backend and what each means.
LABELS = {
    ("backend/app/main.py", "api_router"): "py backend/app/api/main.py/api_router.",
    ("backend/app/main.py", "settings"): "py backend/app/core/config.py/settings.",
    ("backend/app/api/main.py", "items"): "py backend/app/api/routes/items.py/",
    ("backend/app/api/main.py", "login"): "py backend/app/api/routes/login.py/",
    ("backend/app/api/routes/items.py", "SessionDep"): "py backend/app/api/deps.py/SessionDep.",
    ("backend/app/api/routes/items.py", "Item"): "py backend/app/models.py/Item#",
    ("backend/app/api/routes/login.py", "crud"): "py backend/app/crud.py/",
    ("backend/app/api/routes/login.py", "security"): "py backend/app/core/security.py/",
    ("backend/app/api/deps.py", "engine"): "py backend/app/core/db.py/engine.",
    ("backend/app/api/deps.py", "TokenPayload"): "py backend/app/models.py/TokenPayload#",
    ("backend/app/crud.py", "get_password_hash"): (
        "py backend/app/core/security.py/get_password_hash()."
    ),
    ("backend/app/core/db.py", "crud"): "py backend/app/crud.py/",
    ("backend/app/core/db.py", "User"): "py backend/app/models.py/User#",
    ("backend/tests/conftest.py", "init_db"): "py backend/app/core/db.py/init_db().",
    ("backend/tests/conftest.py", "get_superuser_token_headers"): (
        "py backend/tests/utils/utils.py/get_superuser_token_headers()."
    ),
    ("backend/app/api/routes/items.py", "APIRouter"): "external_symbol py:fastapi/APIRouter",
    ("backend/app/core/config.py", "BaseSettings"): (
        "external_symbol py:pydantic_settings/BaseSettings"
    ),
}


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("template")
    with tarfile.open(ARCHIVE) as archive:
        archive.extractall(root, filter="data")
    base = root / "fastapi-template"
    return {
        str(path.relative_to(base)): path.read_text(errors="replace")
        for path in base.rglob("*")
        if path.is_file() and (path.suffix == ".py" or is_config(path.name))
    }


def test_the_template_backend_resolves(template: dict[str, str]) -> None:
    project, facts = _project(template)
    _, _, stats = resolve(project, facts)
    tables = import_tables(project, facts)
    for (path, local), expected in LABELS.items():
        assert tables[f"py {path}/"].get(local) == expected, (path, local)
    backend = [u for u in stats.unresolved if u[0].startswith("backend/")]
    assert stats.internal > 50
    assert stats.rate >= 0.95, backend
    assert not backend
