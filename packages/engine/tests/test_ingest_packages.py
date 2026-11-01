"""Third-party packages (6.5): what the manifests declare, and the lockfiles' versions."""

from __future__ import annotations

import json
from pathlib import Path

from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.packages import inventory, read_lockfiles

PYPROJECT = """
[project]
name = "app"
dependencies = ["FastAPI>=0.115,<1", "SQLModel", "not a requirement !!"]
[project.optional-dependencies]
docs = ["mkdocs"]
s3 = ["boto3>=1"]
[dependency-groups]
dev = ["pytest>=8"]
[tool.uv]
dev-dependencies = ["ruff"]
"""
UV_LOCK = """
version = 1
[[package]]
name = "fastapi"
version = "0.115.6"
[[package]]
name = "sqlmodel"
version = "0.0.22"
[[package]]
name = "pytest"
version = "8.3.4"
"""
PACKAGE_JSON = json.dumps(
    {
        "dependencies": {"react": "^19.1.0", "@tanstack/react-query": "^5"},
        "devDependencies": {"vitest": "^3"},
    }
)
PNPM_LOCK = """
lockfileVersion: '9.0'
importers:
  frontend:
    dependencies:
      react:
        specifier: ^19.1.0
        version: 19.1.0
      '@tanstack/react-query':
        specifier: ^5
        version: 5.62.0(react@19.1.0)
    devDependencies:
      vitest:
        specifier: ^3
        version: 3.0.1
"""


def _by_name(found: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(p["name"]): p for p in found}


def test_declared_packages_with_their_locked_versions() -> None:
    found = inventory(
        {
            "backend/pyproject.toml": PYPROJECT,
            "frontend/package.json": PACKAGE_JSON,
            "backend/requirements-dev.txt": "black==24.1  # formatter\n-r base.txt\n",
        },
        {"backend/uv.lock": UV_LOCK, "pnpm-lock.yaml": PNPM_LOCK},
    )
    python = _by_name([p.as_json() for p in found if p.ecosystem == "pypi"])
    assert python["fastapi"] == {
        "ecosystem": "pypi", "name": "fastapi", "spec": "<1,>=0.115", "version": "0.115.6",
        "manifest": "backend/pyproject.toml", "root": "backend", "dev": False,
    }  # fmt: skip
    assert (python["sqlmodel"]["spec"], python["sqlmodel"]["version"]) == (None, "0.0.22")
    assert python["pytest"]["dev"] is True  # a dependency group
    assert python["ruff"]["dev"] is True
    assert python["mkdocs"]["dev"] is True  # an optional "docs" extra
    assert python["boto3"]["dev"] is False  # an optional feature
    assert python["black"]["dev"] is True  # requirements-dev.txt
    assert python["black"]["version"] is None  # not in the lockfile
    npm = _by_name([p.as_json() for p in found if p.ecosystem == "npm"])
    assert npm["react"]["version"] == "19.1.0"  # the workspace lockfile's importer
    assert npm["@tanstack/react-query"]["version"] == "5.62.0"  # its peers cut off
    assert (npm["vitest"]["dev"], npm["vitest"]["version"]) == (True, "3.0.1")


def test_package_lock_and_bad_files() -> None:
    lock = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "web"},
                "node_modules/react": {"version": "18.3.1"},
                "node_modules/a/node_modules/react": {"version": "17.0.0"},  # nested: not ours
            },
        }
    )
    found = inventory(
        {"package.json": json.dumps({"dependencies": {"react": "^18"}}), "x/package.json": "{"},
        {"package-lock.json": lock, "x/uv.lock": "not toml ["},
    )
    assert [(p.name, p.version, p.root) for p in found] == [("react", "18.3.1", ".")]


def test_discovery_records_the_inventory(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "backend/uv.lock").write_text(UV_LOCK)
    (tmp_path / "backend/main.py").write_text("import fastapi\n")
    found = discover(tmp_path, max_files=100)
    listed = found.summary()["packages"]
    assert {p["name"] for p in listed} >= {"fastapi", "sqlmodel", "pytest"}
    assert read_lockfiles(tmp_path, ["backend/uv.lock", "missing.lock"]) == {
        "backend/uv.lock": UV_LOCK
    }
