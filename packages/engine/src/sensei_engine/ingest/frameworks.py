"""Frameworks and libraries a repository uses, read from its manifests, per package root.

Every manifest counts where it sits, so a monorepo reports ``fastapi`` at ``backend`` and ``react``
at ``frontend`` (the repository root is ``.``). Python dependencies come from ``pyproject.toml``
(PEP 621, PEP 735 groups, Poetry, uv) and ``requirements*.txt``; JavaScript ones from
``package.json``. ``next.config.*`` and ``alembic.ini`` mark Next.js and Alembic by their presence,
Compose files list their services and an ``openapi.{json,yaml,yml}`` is an API description.
``version`` is the declared constraint (``^19.1.0``, ``>=0.115,<1``), not a resolved version.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, cast

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

PYTHON_PACKAGES: dict[str, str] = {
    "fastapi": "fastapi",
    "starlette": "starlette",
    "django": "django",
    "djangorestframework": "django-rest-framework",
    "flask": "flask",
    "sqlalchemy": "sqlalchemy",
    "sqlmodel": "sqlmodel",
    "alembic": "alembic",
    "celery": "celery",
    "pydantic": "pydantic",
    "httpx": "httpx",
    "requests": "requests",
    "aiohttp": "aiohttp",
    "pytest": "pytest",
}

JS_PACKAGES: dict[str, str] = {
    "next": "next",
    "react": "react",
    "vue": "vue",
    "nuxt": "nuxt",
    "svelte": "svelte",
    "@sveltejs/kit": "sveltekit",
    "@angular/core": "angular",
    "express": "express",
    "fastify": "fastify",
    "koa": "koa",
    "hono": "hono",
    "@nestjs/core": "nestjs",
    "prisma": "prisma",
    "@prisma/client": "prisma",
    "typeorm": "typeorm",
    "drizzle-orm": "drizzle",
    "sequelize": "sequelize",
    "mongoose": "mongoose",
    "axios": "axios",
    "@tanstack/react-query": "@tanstack/react-query",
    "swr": "swr",
    "@tanstack/react-router": "@tanstack/react-router",
    "react-router": "react-router",
    "react-router-dom": "react-router",
    "vite": "vite",
    "jest": "jest",
    "vitest": "vitest",
    "@playwright/test": "playwright",
}

_REQUIREMENTS = re.compile(r"^requirements[\w.-]*\.(?:txt|in)$")
_COMPOSE = re.compile(r"^(?:docker-)?compose(?:\.[\w-]+)?\.ya?ml$")
_NEXT_CONFIG = re.compile(r"^next\.config\.(?:js|cjs|mjs|ts|mts)$")
_OPENAPI = re.compile(r"^openapi\.(?:json|ya?ml)$")
ROOT = "."


@dataclass(slots=True)
class Framework:
    name: str
    root: str  # the package directory ("." for the repository root)
    version: str | None = None
    manifests: list[str] = field(default_factory=list[str])
    details: dict[str, Any] = field(default_factory=dict[str, Any])

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"framework": self.name, "version": self.version, "root": self.root}
        data["manifests"] = sorted(self.manifests)
        if self.details:
            data["details"] = self.details
        return data


def _is_requirements(pure: PurePosixPath) -> bool:
    return bool(_REQUIREMENTS.match(pure.name)) or (
        pure.parent.name == "requirements" and pure.suffix in (".txt", ".in")
    )


def is_manifest(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name
    return (
        name in ("pyproject.toml", "package.json", "alembic.ini")
        or _is_requirements(pure)
        or bool(_COMPOSE.match(name) or _NEXT_CONFIG.match(name) or _OPENAPI.match(name))
    )


def _root(path: str) -> str:
    parent = PurePosixPath(path).parent
    if parent.name == "requirements":  # requirements/base.txt belongs to the directory above
        parent = parent.parent
    return str(parent) if str(parent) != "." else ROOT


# ------------------------------------------------------------------ python


_NAME_AND_EXTRAS = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*(?:\[[^\]]*\])?\s*")


def _requirement(line: str) -> tuple[str, str | None] | None:
    """The normalized name and the version constraint as written (``>=0.115,<1``)."""
    try:
        requirement = Requirement(line)
    except InvalidRequirement:
        return None
    if requirement.url or not requirement.specifier:
        return canonicalize_name(requirement.name), None
    written = _NAME_AND_EXTRAS.sub("", line.split(";", 1)[0]).strip().strip("()").strip()
    return canonicalize_name(requirement.name), written or str(requirement.specifier)


def _requirements_txt(text: str) -> Iterable[tuple[str, str | None]]:
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-", "git+", "http:", "https:", "file:")):
            continue  # comments, options (-r, -e, --hash), bare URLs
        parsed = _requirement(line)
        if parsed:
            yield parsed


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast("list[object]", value) if isinstance(item, str)]


def _table(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _poetry_version(spec: object) -> str | None:
    if isinstance(spec, str):
        return spec
    version = _table(spec).get("version")
    return version if isinstance(version, str) else None


def _pyproject(text: str) -> Iterable[tuple[str, str | None]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    project = _table(data.get("project"))
    lines = _strings(project.get("dependencies"))
    for extra in _table(project.get("optional-dependencies")).values():
        lines += _strings(extra)
    for group in _table(data.get("dependency-groups")).values():
        lines += _strings(group)  # {include-group = ...} entries aren't strings
    tool = _table(data.get("tool"))
    lines += _strings(_table(tool.get("uv")).get("dev-dependencies"))
    for line in lines:
        parsed = _requirement(line)
        if parsed:
            yield parsed
    poetry = _table(tool.get("poetry"))
    tables = [_table(poetry.get("dependencies")), _table(poetry.get("dev-dependencies"))]
    tables += [_table(_table(g).get("dependencies")) for g in _table(poetry.get("group")).values()]
    for table in tables:
        for name, spec in table.items():
            if name != "python":
                yield canonicalize_name(name), _poetry_version(spec)


# ------------------------------------------------------------------ javascript


def _package_json(text: str) -> Iterable[tuple[str, str | None]]:
    try:
        data = json.loads(text)
    except ValueError:
        return
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, spec in _table(_table(data).get(key)).items():
            yield name, spec if isinstance(spec, str) else None


# ------------------------------------------------------------------ the rest


def _yaml(text: str) -> object:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _compose_services(text: str) -> list[str]:
    services = _table(_table(_yaml(text)).get("services"))
    return sorted(str(name) for name in services)


def _openapi_version(path: str, text: str) -> str | None:
    if path.endswith(".json"):
        try:
            data: object = json.loads(text)
        except ValueError:
            return None
    else:
        data = _yaml(text)
    version = _table(data).get("openapi") or _table(data).get("swagger")
    return str(version) if version else None


class _Found:
    def __init__(self) -> None:
        self.by_key: dict[tuple[str, str], Framework] = {}

    def add(
        self,
        name: str,
        root: str,
        manifest: str,
        version: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Framework:
        framework = self.by_key.setdefault((name, root), Framework(name, root))
        if version and not framework.version:
            framework.version = version
        if manifest not in framework.manifests:
            framework.manifests.append(manifest)
        for key, value in (details or {}).items():
            if isinstance(value, list):
                merged = set(cast("list[str]", framework.details.get(key, [])))
                framework.details[key] = sorted(merged.union(cast("list[str]", value)))
            else:
                framework.details[key] = value
        return framework


def detect_frameworks(manifests: Mapping[str, str]) -> list[Framework]:
    """Frameworks from the manifests' text (path → content), sorted by root, then name."""
    found = _Found()
    for path in sorted(manifests):
        text = manifests[path]
        name = PurePosixPath(path).name
        root = _root(path)
        if name == "pyproject.toml" or _is_requirements(PurePosixPath(path)):
            parse = _pyproject if name == "pyproject.toml" else _requirements_txt
            for package, version in parse(text):
                if package in PYTHON_PACKAGES:
                    found.add(PYTHON_PACKAGES[package], root, path, version)
        elif name == "package.json":
            for package, version in _package_json(text):
                if package in JS_PACKAGES:
                    found.add(JS_PACKAGES[package], root, path, version)
        elif _NEXT_CONFIG.match(name):
            found.add("next", root, path)
        elif name == "alembic.ini":
            found.add("alembic", root, path)
        elif _COMPOSE.match(name):
            found.add("docker-compose", root, path, details={"services": _compose_services(text)})
        elif _OPENAPI.match(name):
            found.add("openapi", root, path, _openapi_version(path, text))
    return sorted(found.by_key.values(), key=lambda f: (f.root != ROOT, f.root, f.name))


def framework_root(path: str, roots: Iterable[str]) -> str | None:
    """The deepest framework root that contains ``path``."""
    best: str | None = None
    for root in roots:
        inside = root == ROOT or path.startswith(f"{root}/")
        if inside and (best is None or best == ROOT or len(root) > len(best)):
            best = root
    return best
