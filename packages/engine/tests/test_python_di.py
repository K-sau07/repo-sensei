"""FastAPI dependencies as INJECTS edges and Python tests as test nodes with TESTS edges (3.5):
every way FastAPI declares a dependency on a small project, and the fastapi-template's 41,
checked by hand against its routes."""

from __future__ import annotations

import tarfile
from collections.abc import Mapping
from pathlib import Path

from sensei_core.models import EdgeType, NodeKind
from sensei_engine.graph import python as py
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.python.di import Injection, extract
from sensei_engine.graph.python.tests import is_test, is_test_file
from sensei_engine.graph.writer import CODE, GraphEdge, GraphNode
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

ARCHIVE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"

FILES = {
    "pyproject.toml": '[project]\nname = "shop"\n',
    "shop/__init__.py": "",
    "shop/deps.py": (
        "from typing import Annotated\n"
        "from fastapi import Depends, Security\n"
        "from sqlmodel import Session\n"
        "\n"
        "\n"
        "def get_db():\n"
        "    pass\n"
        "\n"
        "\n"
        "SessionDep = Annotated[Session, Depends(get_db)]\n"
        "\n"
        "\n"
        "def get_user(session: SessionDep):\n"
        "    pass\n"
        "\n"
        "\n"
        "def admin(user=Depends(get_user)):\n"
        "    pass\n"
        "\n"
        "\n"
        "class Paging:\n"
        "    pass\n"
    ),
    "shop/routes.py": (
        "from typing import Annotated, Optional\n"
        "from fastapi import APIRouter, Depends, Security\n"
        "from fastapi.security import OAuth2PasswordRequestForm\n"
        "from shop import deps\n"
        "from shop.deps import SessionDep, get_user, Paging\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        '@router.get("/", dependencies=[Depends(deps.admin)])\n'
        "def read(\n"
        "    session: SessionDep,\n"
        "    other: deps.SessionDep,\n"
        "    user: Annotated[object, Depends(get_user)],\n"
        "    form: Annotated[OAuth2PasswordRequestForm, Depends()],\n"
        "    page: Paging = Depends(),\n"
        '    scoped=Security(get_user, scopes=["a"]),\n'
        "    maybe: Optional[SessionDep] = None,\n"
        "    plain: int = 0,\n"
        "):\n"
        "    pass\n"
        "\n"
        "\n"
        "class Views:\n"
        "    def show(self, session: SessionDep):\n"
        "        pass\n"
    ),
    "tests/test_routes.py": (
        "from shop.routes import read\n"
        "from shop.deps import get_db\n"
        "\n"
        "\n"
        "def test_read():\n"
        "    read()\n"
        "    get_db()\n"
        "    len([])\n"
        "\n"
        "\n"
        "class TestViews:\n"
        "    def test_show(self):\n"
        "        read()\n"
        "\n"
        "    def helper(self):\n"
        "        read()\n"
        "\n"
        "\n"
        "def helper():\n"
        "    read()\n"
    ),
}


def _resolve(
    files: Mapping[str, str],
) -> tuple[list[GraphNode], list[GraphEdge], py.PyStats]:
    definitions: dict[str, list[Definition]] = {}
    facts: dict[str, py.PyFileFacts] = {}
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        parsed = parse_file(path, content.encode())
        found = py.extract(parsed)
        assert found is not None
        facts[path] = found
        definitions[path] = [
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, "python")
        ]
    project = Project(
        paths=frozenset(files),
        configs={path: content for path, content in files.items() if is_config(path)},
        definitions=definitions,
    )
    nodes, edges, stats = py.resolve(project, facts)
    CODE.check(nodes, edges)
    return nodes, edges, stats


def _injects(edges: list[GraphEdge]) -> set[tuple[str, str, int]]:
    return {
        (e.src.removeprefix("py "), e.dst.removeprefix("py "), e.evidence_line or 0)
        for e in edges
        if e.type is EdgeType.INJECTS
    }


def test_depends_is_read_from_defaults_annotations_aliases_and_decorators() -> None:
    found = extract(parse_file("shop/routes.py", FILES["shop/routes.py"].encode())).injections
    assert [(i.kind, i.line, i.target, i.function) for i in found if i.kind == "depends"] == [
        ("depends", 10, ("deps", "admin"), "read"),  # the decorator's
        ("depends", 14, ("get_user",), "read"),
        ("depends", 15, ("OAuth2PasswordRequestForm",), "read"),  # Depends(): the type
        ("depends", 16, ("Paging",), "read"),
        ("depends", 17, ("get_user",), "read"),  # Security
    ]
    aliases = extract(parse_file("shop/deps.py", FILES["shop/deps.py"].encode())).injections
    assert Injection("alias", 10, ("get_db",), alias="SessionDep") in aliases


def test_injects_edges_bind_dependencies_and_aliases_across_files() -> None:
    nodes, edges, stats = _resolve(FILES)
    assert _injects(edges) == {
        ("shop/deps.py/get_user().", "shop/deps.py/get_db().", 13),  # through the alias
        ("shop/deps.py/admin().", "shop/deps.py/get_user().", 17),
        ("shop/routes.py/read().", "shop/deps.py/admin().", 10),
        ("shop/routes.py/read().", "shop/deps.py/get_db().", 12),  # SessionDep, imported
        ("shop/routes.py/read().", "shop/deps.py/get_db().", 13),  # deps.SessionDep
        ("shop/routes.py/read().", "shop/deps.py/get_user().", 14),
        (
            "shop/routes.py/read().",
            "external_symbol py:fastapi.security/OAuth2PasswordRequestForm",
            15,
        ),
        ("shop/routes.py/read().", "shop/deps.py/Paging#", 16),
        ("shop/routes.py/read().", "shop/deps.py/get_user().", 17),
        ("shop/routes.py/read().", "shop/deps.py/get_db().", 18),  # Optional[SessionDep]
        ("shop/routes.py/Views#show().", "shop/deps.py/get_db().", 25),  # a method's
    }  # plain: int is an annotation, not a dependency
    assert stats.di.unresolved == []
    assert any(
        n.key == "external_symbol py:fastapi.security/OAuth2PasswordRequestForm" for n in nodes
    )


def test_python_test_functions_are_test_nodes_with_tests_edges() -> None:
    nodes, edges, stats = _resolve(FILES)
    tests = {
        n.key: (n.name, n.attrs["function"], n.owner) for n in nodes if n.kind is NodeKind.TEST
    }
    assert tests == {
        "test tests/test_routes.py:5": (
            "test_read",
            "py tests/test_routes.py/test_read().",
            "tests/test_routes.py",
        ),
        "test tests/test_routes.py:12": (
            "TestViews.test_show",
            "py tests/test_routes.py/TestViews#test_show().",
            "tests/test_routes.py",
        ),
    }  # helpers aren't tests
    assert {
        (e.src, e.dst, e.evidence_line, e.confidence) for e in edges if e.type is EdgeType.TESTS
    } == {
        ("test tests/test_routes.py:5", "py shop/routes.py/read().", 6, 0.7),
        ("test tests/test_routes.py:5", "py shop/deps.py/get_db().", 7, 0.7),
        ("test tests/test_routes.py:12", "py shop/routes.py/read().", 13, 0.7),
    }  # len is a builtin: nothing to test
    assert (stats.tests.tests, stats.tests.edges) == (2, 3)


def test_test_files_and_functions_are_pytest_s() -> None:
    assert is_test_file("tests/api/routes.py")
    assert is_test_file("app/test_items.py")
    assert is_test_file("app/items_test.py")
    assert not is_test_file("app/api/routes/login.py")
    assert not is_test_file("tests/fixtures/data.json")
    assert is_test(Definition("k", "test_x", "TestA.test_x", "method", 1, 2))
    assert not is_test(Definition("k", "test_x", "Helper.test_x", "method", 1, 2))
    assert not is_test(Definition("k", "test_x", "test_x", "variable", 1, 2))


def test_fastapi_template_dependencies() -> None:
    files: dict[str, str] = {}
    with tarfile.open(ARCHIVE) as archive:
        for member in archive.getmembers():
            handle = archive.extractfile(member) if member.isfile() else None
            path = member.name.removeprefix("fastapi-template/")
            if handle is not None and (path.endswith(".py") or is_config(path)):
                files[path] = handle.read().decode()
    _, edges, stats = _resolve(files)
    injects = _injects(edges)
    # Checked by hand against backend/app/api: every SessionDep, CurrentUser and TokenDep
    # parameter, every Depends in a decorator, and the login form's Depends().
    assert len(injects) == 41
    assert stats.di.unresolved == []
    deps = "backend/app/api/deps.py"
    assert {(src, dst) for src, dst, _ in injects if src.startswith(deps)} == {
        (f"{deps}/get_current_user().", f"{deps}/get_db()."),
        (f"{deps}/get_current_user().", f"{deps}/reusable_oauth2."),
        (f"{deps}/get_current_active_superuser().", f"{deps}/get_current_user()."),
    }
    routes = "backend/app/api/routes"
    assert {dst for src, dst, _ in injects if src == f"{routes}/users.py/delete_user()."} == {
        f"{deps}/get_current_active_superuser().",  # the decorator's
        f"{deps}/get_db().",
        f"{deps}/get_current_user().",
    }
    assert (
        f"{routes}/login.py/login_access_token().",
        "external_symbol py:fastapi.security/OAuth2PasswordRequestForm",
        25,
    ) in injects
