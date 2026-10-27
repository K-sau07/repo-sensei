"""FastAPI routes to endpoints: the static reading (decorators, prefixes across modules), the
reconciling with a runtime answer, and the fastapi-template's routes against a hand-made list;
then the running app in the sandbox (``integration``: Docker; the template's needs pypi.org)."""

from __future__ import annotations

import subprocess
import tarfile
import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.python.imports import PyFacts, extract
from sensei_engine.graph.writer import LINKS
from sensei_engine.link.fastapi import (
    STATIC_CONFIDENCE,
    FileRoutes,
    Route,
    RuntimeRoutes,
    app_entries,
    endpoint_facts,
    normalize_path,
    runtime_routes,
    scan_routes,
    static_routes,
)
from sensei_engine.parse.parser import parse_file
from sensei_engine.sandbox.deps import DependencyImages
from sensei_engine.sandbox.runner import Sandbox, docker_env, runner_image
from sensei_engine.symbols.extract import extract_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"

REPO = {
    "svc/pyproject.toml": '[project]\nname = "svc"\n',
    "svc/app/__init__.py": "",
    "svc/app/config.py": """
        API = "/api"

        class Settings:
            V2: str = "/v2"
            name = "x"

        settings = Settings()
    """,
    "svc/app/main.py": """
        from fastapi import FastAPI

        from app.config import API, settings
        from app.routers import items, admin as admin_routes
        from app.routers.users import router as users_router
        from app.sub import subapp

        app = FastAPI()
        app.include_router(items.router, prefix=API)
        app.include_router(users_router, prefix=f"{API}{settings.V2}")
        app.mount("/sub", subapp)

        if True:
            app.include_router(admin_routes.router, prefix="/admin")

        app.include_router(items.router, prefix=PREFIX_FROM_NOWHERE)


        @app.get("/health")
        def health():
            return "ok"


        @app.websocket("/ws")
        async def stream(ws):
            pass


        def build_settings():
            return {}.get("x")
    """,
    "svc/app/routers/__init__.py": "",
    "svc/app/routers/items.py": """
        from fastapi import APIRouter

        router = APIRouter(prefix="/items", tags=["items"])


        @router.get("/")
        def list_items():
            pass


        @router.api_route("/{item_id}", methods=["GET", "put"])
        def item(item_id: int):
            pass


        def remove(item_id: int):
            pass


        router.add_api_route("/{item_id}", remove, methods=["DELETE"])
    """,
    "svc/app/routers/users.py": """
        from fastapi import APIRouter

        from .nested import router as nested

        router = APIRouter(prefix="/users")
        router.include_router(nested, prefix="/{user_id}")


        @router.post("")
        def create_user():
            pass
    """,
    "svc/app/routers/nested.py": """
        from fastapi import APIRouter

        router = APIRouter()


        @router.get("/files/{name:path}")
        def user_file(user_id: int, name: str):
            pass
    """,
    "svc/app/routers/admin.py": """
        from fastapi import APIRouter

        router = APIRouter()


        @router.delete("/cache")
        def clear_cache():
            pass
    """,
    "svc/app/sub.py": """
        from fastapi import FastAPI

        subapp = FastAPI()


        @subapp.get("/ping")
        def ping():
            pass
    """,
    "tools/orphan.py": """
        from fastapi import APIRouter

        router = APIRouter(prefix="/orphan")


        @router.get("/x")
        def orphan():
            pass
    """,
}


def _project(files: Mapping[str, str]) -> tuple[Project, dict[str, PyFacts], dict[str, FileRoutes]]:
    definitions: dict[str, list[Definition]] = {}
    facts: dict[str, PyFacts] = {}
    routes: dict[str, FileRoutes] = {}
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        parsed = parse_file(path, content.encode())
        definitions[path] = [
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, "python")
        ]
        facts[path] = extract(parsed)
        routes[path] = scan_routes(parsed, definitions[path])
    configs = {path: content for path, content in files.items() if is_config(path)}
    return Project(frozenset(files), configs, definitions), facts, routes


@pytest.fixture(scope="module")
def repo() -> tuple[Project, dict[str, PyFacts], dict[str, FileRoutes]]:
    return _project({path: textwrap.dedent(text).lstrip() for path, text in REPO.items()})


def _table(routes: list[Route]) -> dict[tuple[str, str], tuple[str | None, bool, bool]]:
    return {
        (r.method, r.path): (
            r.handler.split("/")[-1] if r.handler else None,
            r.reachable,
            bool(r.unresolved),
        )
        for r in routes
    }


def test_static_routes_follow_prefixes_across_modules(
    repo: tuple[Project, dict[str, PyFacts], dict[str, FileRoutes]],
) -> None:
    project, facts, files = repo
    routes = static_routes(project, files, facts)
    table = _table(routes)
    assert table == {
        ("GET", "/api/items/"): ("list_items().", True, False),  # module constant
        ("GET", "/api/items/{item_id}"): ("item().", True, False),  # api_route(methods=...)
        ("PUT", "/api/items/{item_id}"): ("item().", True, False),
        ("DELETE", "/api/items/{item_id}"): ("remove().", True, False),  # add_api_route
        # The same router again, under a prefix that isn't a constant.
        ("GET", "/items/"): ("list_items().", True, True),
        ("GET", "/items/{item_id}"): ("item().", True, True),
        ("PUT", "/items/{item_id}"): ("item().", True, True),
        ("DELETE", "/items/{item_id}"): ("remove().", True, True),
        # f"{API}{settings.V2}" + the router's prefix + "" (router.post(""))
        ("POST", "/api/v2/users"): ("create_user().", True, False),
        # Nested: an include inside an included router, relative import, path catch-all.
        ("GET", "/api/v2/users/{user_id}/files/{name:path}"): ("user_file().", True, False),
        ("DELETE", "/admin/cache"): ("clear_cache().", True, False),  # behind an if
        ("GET", "/health"): ("health().", True, False),
        ("WS", "/ws"): ("stream().", True, False),
        ("GET", "/sub/ping"): ("ping().", True, False),  # a mounted app
        ("GET", "/orphan/x"): ("orphan().", False, False),  # no app includes it
    }
    conditional = {(r.method, r.path) for r in routes if r.conditional}
    assert conditional == {("DELETE", "/admin/cache")}
    entries = app_entries(project, files, facts)
    assert [(e.root, e.target) for e in entries] == [("svc", "app.main:app")]  # not the mount


@pytest.mark.parametrize(
    ("path", "normalized"),
    [
        ("/items/", "/items"),
        ("/items/{id}", "/items/{}"),
        ("/users/{user_id}/files/{name:path}", "/users/{}/files/{*}"),
        ("/a//b/", "/a/b"),
        ("", "/"),
        ("/", "/"),
        ("items", "/items"),
        ("/x/{n:int}", "/x/{}"),
    ],
)
def test_paths_normalize(path: str, normalized: str) -> None:
    assert normalize_path(path) == normalized


def test_an_apps_root_path_is_on_its_endpoints() -> None:
    project, facts, files = _project(
        {
            "app/main.py": textwrap.dedent(
                """
                from fastapi import APIRouter, FastAPI
                from app.config import ROOT

                router = APIRouter(prefix="/items")

                @router.get("/{id}")
                def read(id: int): ...

                app = FastAPI(root_path=ROOT)
                app.include_router(router)
                """
            ),
            "app/config.py": 'ROOT = "/backend/"\n',
        }
    )
    graph = endpoint_facts(project, static_routes(project, files, facts), None)
    assert [(n.key, n.attrs.get("root_path")) for n in graph.nodes] == [
        ("api_endpoint GET /items/{}", "/backend")
    ]


def test_without_a_runtime_answer_static_routes_stand(
    repo: tuple[Project, dict[str, PyFacts], dict[str, FileRoutes]],
) -> None:
    project, facts, files = repo
    static = static_routes(project, files, facts)
    failed = RuntimeRoutes([], limitation="no module named fastapi")
    graph = endpoint_facts(project, static, failed)
    LINKS.check(graph.nodes, graph.edges)
    assert graph.report.limitation == "no module named fastapi"
    nodes = {n.key: n for n in graph.nodes}
    health = nodes["api_endpoint GET /health"]
    assert health.attrs["source"] == "static"
    assert health.attrs["path_raw"] == "/health"
    assert health.path == "svc/app/main.py"
    edges = {e.src: e for e in graph.edges}
    assert edges["api_endpoint GET /health"].dst == "py svc/app/main.py/health()."
    assert edges["api_endpoint GET /health"].provenance is Provenance.IMPORT_RESOLVED
    assert edges["api_endpoint GET /health"].confidence == pytest.approx(0.9)
    # Not sure of: a router no app includes, a prefix that isn't a constant.
    assert edges["api_endpoint GET /orphan/x"].confidence == STATIC_CONFIDENCE
    assert edges["api_endpoint GET /items"].confidence == STATIC_CONFIDENCE
    assert nodes["api_endpoint GET /items"].attrs["unresolved_prefix"] == ["PREFIX_FROM_NOWHERE"]
    assert nodes["api_endpoint GET /api/v2/users/{}/files/{*}"].attrs["path_norm"] == (
        "/api/v2/users/{}/files/{*}"
    )
    assert all(e.type is EdgeType.HANDLED_BY for e in graph.edges)


def test_the_runtime_wins_and_static_only_routes_drop(
    repo: tuple[Project, dict[str, PyFacts], dict[str, FileRoutes]],
) -> None:
    project, facts, files = repo
    static = static_routes(project, files, facts)
    runtime = RuntimeRoutes(
        [
            Route(
                "GET",
                "/health",
                "py svc/app/main.py/health().",
                "runtime",
                evidence_path="svc/app/main.py",
                name="health",
                operation_id="health_get",
                entry="app.main:app",
            )
        ]
    )
    graph = endpoint_facts(project, static, runtime)
    nodes = {n.key: n for n in graph.nodes}
    edges = {e.src: e for e in graph.edges}
    assert nodes["api_endpoint GET /health"].attrs["source"] == "runtime"
    assert nodes["api_endpoint GET /health"].attrs["operation_id"] == "health_get"
    assert edges["api_endpoint GET /health"].provenance is Provenance.RUNTIME
    assert edges["api_endpoint GET /health"].confidence == 1.0
    cache = nodes["api_endpoint DELETE /admin/cache"]
    assert cache.attrs["runtime_missing"] is True  # the running app hasn't it: kept, doubted
    assert cache.attrs["conditional"] is True
    assert edges["api_endpoint DELETE /admin/cache"].confidence == STATIC_CONFIDENCE
    assert graph.report.runtime == 1
    assert graph.report.static_only == len(graph.nodes) - 1


# ---------------------------------------------------------------- the fastapi-template

TEMPLATE_ROUTES = {
    ("POST", "/api/v1/login/access-token", "login.py/login_access_token()."),
    ("POST", "/api/v1/login/test-token", "login.py/test_token()."),
    ("POST", "/api/v1/password-recovery/{email}", "login.py/recover_password()."),
    ("POST", "/api/v1/reset-password/", "login.py/reset_password()."),
    (
        "POST",
        "/api/v1/password-recovery-html-content/{email}",
        "login.py/recover_password_html_content().",
    ),
    ("GET", "/api/v1/users/", "users.py/read_users()."),
    ("POST", "/api/v1/users/", "users.py/create_user()."),
    ("PATCH", "/api/v1/users/me", "users.py/update_user_me()."),
    ("PATCH", "/api/v1/users/me/password", "users.py/update_password_me()."),
    ("GET", "/api/v1/users/me", "users.py/read_user_me()."),
    ("DELETE", "/api/v1/users/me", "users.py/delete_user_me()."),
    ("POST", "/api/v1/users/signup", "users.py/register_user()."),
    ("GET", "/api/v1/users/{user_id}", "users.py/read_user_by_id()."),
    ("PATCH", "/api/v1/users/{user_id}", "users.py/update_user()."),
    ("DELETE", "/api/v1/users/{user_id}", "users.py/delete_user()."),
    ("POST", "/api/v1/utils/test-email/", "utils.py/test_email()."),
    ("GET", "/api/v1/utils/health-check/", "utils.py/health_check()."),
    ("GET", "/api/v1/items/", "items.py/read_items()."),
    ("GET", "/api/v1/items/{id}", "items.py/read_item()."),
    ("POST", "/api/v1/items/", "items.py/create_item()."),
    ("PUT", "/api/v1/items/{id}", "items.py/update_item()."),
    ("DELETE", "/api/v1/items/{id}", "items.py/delete_item()."),
    ("POST", "/api/v1/private/users/", "private.py/create_user()."),  # included behind an if
}


def template_project(root: Path) -> tuple[Project, dict[str, PyFacts], dict[str, FileRoutes]]:
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(root, filter="data")
    base = root / "fastapi-template"
    return _project(
        {
            str(path.relative_to(base)): path.read_text(errors="replace")
            for path in base.rglob("*")
            if path.is_file() and (path.suffix == ".py" or is_config(path.name))
        }
    )


def as_template_routes(routes: list[Route]) -> set[tuple[str, str, str]]:
    prefix = "py backend/app/api/routes/"
    return {(r.method, r.path, (r.handler or "").removeprefix(prefix)) for r in routes}


def test_the_template_routes_statically(tmp_path: Path) -> None:
    project, facts, files = template_project(tmp_path)
    routes = static_routes(project, files, facts)
    assert as_template_routes(routes) == TEMPLATE_ROUTES
    assert all(r.reachable and not r.unresolved for r in routes)  # settings.API_V1_STR resolved
    assert [r.path for r in routes if r.conditional] == ["/api/v1/private/users/"]
    entries = app_entries(project, files, facts)
    assert [(e.root, e.target) for e in entries] == [("backend", "app.main:app")]
    graph = endpoint_facts(project, routes, None)
    LINKS.check(graph.nodes, graph.edges)
    assert graph.report.endpoints == 23
    assert graph.report.no_handler == 0


# ---------------------------------------------------------------- at runtime, in the sandbox


@pytest.fixture(scope="module")
def sandbox() -> Sandbox:
    try:
        listed = subprocess.run(  # noqa: S603 - a fixed argv, no shell
            ["docker", "image", "inspect", runner_image("3.14"), runner_image()],  # noqa: S607
            capture_output=True,
            timeout=30,
            env=docker_env(),
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("docker isn't installed")
    if listed.returncode != 0:
        pytest.skip("no runner images: make sandbox-image")
    return Sandbox()


@pytest.mark.integration
async def test_an_app_that_does_not_import_is_a_limitation(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = (
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/x')\ndef x():\n    pass\n"
    )
    (root / "main.py").write_text(source)
    project, facts, files = _project({"main.py": source})
    entries = app_entries(project, files, facts)
    assert [(e.root, e.target) for e in entries] == [("", "main:app")]
    runtime = await runtime_routes(sandbox, DependencyImages(sandbox), root, project, entries)
    assert runtime.routes == []
    assert runtime.limitation is not None
    assert "ModuleNotFoundError" in runtime.limitation  # no dependencies declared: no fastapi
    graph = endpoint_facts(project, static_routes(project, files, facts), runtime)
    assert [n.key for n in graph.nodes] == ["api_endpoint GET /x"]  # the static route stands


@pytest.mark.integration
@pytest.mark.slow
async def test_the_template_routes_at_runtime(sandbox: Sandbox, tmp_path: Path) -> None:
    project, facts, files = template_project(tmp_path)
    runtime = await runtime_routes(
        sandbox,
        DependencyImages(sandbox),
        tmp_path / "fastapi-template",
        project,
        app_entries(project, files, facts),
    )
    assert runtime.limitation is None, runtime.notes
    assert as_template_routes(runtime.routes) == TEMPLATE_ROUTES
    assert all(r.operation_id for r in runtime.routes)  # app.openapi() ran
    graph = endpoint_facts(project, static_routes(project, files, facts), runtime)
    assert graph.report.endpoints == 23
    assert graph.report.static_only == 0  # static and runtime agree on every route
    assert {e.confidence for e in graph.edges} == {1.0}
