"""Frameworks from manifests (per package root) and file roles."""

from __future__ import annotations

import json

import pytest

from sensei_engine.ingest.frameworks import detect_frameworks, framework_root, is_manifest
from sensei_engine.ingest.roles import Role, assign_role


def _found(manifests: dict[str, str]) -> dict[tuple[str, str], str | None]:
    return {(f.root, f.name): f.version for f in detect_frameworks(manifests)}


def test_pyproject_dependencies_in_every_form() -> None:
    pyproject = """
[project]
dependencies = ["fastapi[standard]>=0.115,<1", "SQLAlchemy[asyncio] (>=2.0)", "requests"]
[project.optional-dependencies]
worker = ["celery>=5.4; python_version >= '3.11'"]
[dependency-groups]
dev = ["pytest>=8", {include-group = "lint"}]
lint = ["ruff"]
[tool.poetry.dependencies]
python = "^3.12"
Django = {version = "^5.1", extras = ["argon2"]}
[tool.poetry.group.web.dependencies]
flask = "*"
"""
    assert _found({"pyproject.toml": pyproject}) == {
        (".", "fastapi"): ">=0.115,<1",
        (".", "sqlalchemy"): ">=2.0",
        (".", "requests"): None,
        (".", "celery"): ">=5.4",
        (".", "pytest"): ">=8",
        (".", "django"): "^5.1",
        (".", "flask"): "*",
    }


def test_requirements_files() -> None:
    manifests = {
        "svc/requirements.txt": "# web\nflask==3.0.0  # pinned\n-r base.txt\n--hash=sha256:x\n",
        "svc/requirements/dev.txt": "pytest\ngit+https://github.com/x/y.git\n",
        "svc/requirements-celery.txt": "celery[redis]>=5\nnot a requirement !!\n",
    }
    assert all(is_manifest(path) for path in manifests)
    assert _found(manifests) == {
        ("svc", "flask"): "==3.0.0",
        ("svc", "pytest"): None,
        ("svc", "celery"): ">=5",
    }


def test_package_json_and_config_files_per_root() -> None:
    web = {
        "dependencies": {"next": "15.1.0", "react": "^19", "@tanstack/react-query": "^5"},
        "devDependencies": {"vitest": "^2", "typescript": "^5"},
    }
    api = {"dependencies": {"express": "^4", "@prisma/client": "^6"}}
    manifests = {
        "package.json": json.dumps({"private": True, "workspaces": ["apps/*"]}),
        "apps/web/package.json": json.dumps(web),
        "apps/web/next.config.ts": "export default {}",
        "apps/api/package.json": json.dumps(api),
        "apps/api/prisma/package.json": "{not json",
    }
    assert _found(manifests) == {
        ("apps/api", "express"): "^4",
        ("apps/api", "prisma"): "^6",
        ("apps/web", "next"): "15.1.0",
        ("apps/web", "react"): "^19",
        ("apps/web", "@tanstack/react-query"): "^5",
        ("apps/web", "vitest"): "^2",
    }
    next_js = next(f for f in detect_frameworks(manifests) if f.name == "next")
    assert next_js.manifests == ["apps/web/next.config.ts", "apps/web/package.json"]


def test_alembic_compose_and_openapi() -> None:
    manifests = {
        "backend/alembic.ini": "[alembic]\nscript_location = app/alembic\n",
        "compose.yml": "services:\n  db:\n    image: postgres\n  api:\n    build: .\n",
        "compose.override.yml": "services:\n  mailpit:\n    image: axllent/mailpit\n",
        "docker-compose.yaml": ": not yaml : [",
        "api/openapi.yaml": "openapi: 3.1.0\ninfo: {title: x}\n",
        "spec/openapi.json": json.dumps({"swagger": "2.0"}),
    }
    frameworks = {(f.root, f.name): f for f in detect_frameworks(manifests)}
    assert set(frameworks) == {
        ("backend", "alembic"),
        (".", "docker-compose"),
        ("api", "openapi"),
        ("spec", "openapi"),
    }
    assert frameworks[".", "docker-compose"].details == {"services": ["api", "db", "mailpit"]}
    assert frameworks["api", "openapi"].version == "3.1.0"
    assert frameworks["spec", "openapi"].version == "2.0"


def test_not_manifests() -> None:
    for path in ("src/package.ts", "requirements.md", "docs/openapi.md", "compose.ts"):
        assert not is_manifest(path)


def test_framework_root_is_the_deepest() -> None:
    roots = [".", "apps/web", "apps"]
    assert framework_root("apps/web/src/page.tsx", roots) == "apps/web"
    assert framework_root("apps/api/main.py", roots) == "apps"
    assert framework_root("README.md", roots) == "."
    assert framework_root("apps/webby/x.ts", ["apps/web"]) is None


# ------------------------------------------------------------------ roles


@pytest.mark.parametrize(
    ("path", "language", "text", "ui_package", "role"),
    [
        ("backend/tests/test_items.py", "python", "", False, Role.TEST),
        ("src/app.test.tsx", "tsx", "", True, Role.TEST),
        ("pkg/thing_test.go", "go", "", False, Role.TEST),
        ("conftest.py", "python", "", False, Role.TEST),
        ("README.md", "markdown", "", False, Role.DOCS),
        ("LICENSE", "text", "", False, Role.DOCS),
        ("docs/conf.py", "python", "", False, Role.DOCS),
        ("app/core/security.py", "python", "", False, Role.SERVICE),  # not a SECURITY doc
        ("tsconfig.json", "json", "", True, Role.CONFIG),
        ("vite.config.ts", "typescript", "", True, Role.CONFIG),
        ("Dockerfile", "dockerfile", "", False, Role.CONFIG),
        (".github/workflows/ci.yml", "yaml", "", False, Role.CONFIG),
        ("app/core/config.py", "python", "", False, Role.CONFIG),
        ("scripts/release.py", "python", "", False, Role.CONFIG),
        ("schema.sql", "sql", "", False, Role.DB),
        ("app/models.py", "python", "", False, Role.DB),
        ("app/alembic/versions/1a_init.py", "python", "", False, Role.DB),
        ("src/user.entity.ts", "typescript", "", False, Role.DB),
        ("app/tables.py", "python", "class User(SQLModel, table=True):\n", False, Role.DB),
        ("app/api/routes/items.py", "python", "", False, Role.API),
        ("app/main.py", "python", "app = FastAPI(title='x')\n", False, Role.API),
        ("svc/handlers.py", "python", "@app.get('/items')\ndef items(): ...\n", False, Role.API),
        ("server/index.js", "javascript", "app.get('/health', ok)\n", False, Role.API),
        ("web/app/api/users/route.ts", "typescript", "", True, Role.API),
        ("web/pages/api/hello.ts", "typescript", "", True, Role.API),
        ("web/src/routes/login.tsx", "tsx", "return (<Form />)", True, Role.UI),
        ("web/src/api/client.ts", "typescript", "", True, Role.UI),  # frontend's API client
        ("web/src/hooks/useAuth.ts", "typescript", "", True, Role.UI),
        ("Button.tsx", "tsx", "export const B = () => <button />", False, Role.UI),
        ("styles/site.css", "css", "", False, Role.UI),
        ("templates/email.html", "html", "", False, Role.UI),
        ("app/core/security.py", "python", "", True, Role.UI),
        ("app/utils.py", "python", "", False, Role.SERVICE),
        ("img/logo.png", None, None, False, Role.OTHER),
        ("data/cities.csv", "csv", "", False, Role.OTHER),
    ],
)
def test_roles(
    path: str, language: str | None, text: str | None, ui_package: bool, role: Role
) -> None:
    assert assign_role(path, language, text, ui_package=ui_package) is role
