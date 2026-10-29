"""What part of the application a file belongs to (heuristic v1; the code graph refines it later).

Checked in order, first match wins:

1. ``test``: test directories and test file names (``test_*.py``, ``*.spec.ts``, ``conftest.py``);
2. ``docs``: prose (Markdown, reStructuredText), READMEs, licences and changelogs, ``docs/``;
3. ``config``: data and build files (JSON, YAML, TOML, Dockerfiles, CI), ``*.config.*``,
   dotfiles, settings modules and scripts;
4. ``db``: SQL, schemas and migrations, models and repositories (Rails' and Laravel's
   ``app/models``, ASP.NET's ``Models``, Go's and Rust's ``models``, Rust's ``db``); JVM code
   (Java, Kotlin) by the package it is in too: ``entity``, ``model``, ``domain``, ``dao`` and
   the like; a folder only one ecosystem gives the layer: C#'s ``Entities``, Go's ``store``;
5. ``api``: route handlers and applications by framework decorator or registration anywhere,
   and by directory (``api``, ``routes``, ``controllers``, ``endpoints``, ``handlers``: Rails',
   Laravel's and ASP.NET's controllers, Go's and Rust's handlers) outside frontend packages;
   JVM code in a ``controller``, ``web`` or ``rest`` package;
6. ``ui``: components that return JSX, Vue/Svelte/Astro files, stylesheets, HTML, and any
   JavaScript or TypeScript in a frontend package (one whose manifest uses React, Vue, Svelte,
   Angular or Next.js), except its server endpoints (Next.js ``route.ts``, SvelteKit
   ``+server.ts``);
7. ``service``: the rest of the code;
8. ``other``: everything else (images, data, plain text).
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from sensei_engine.ingest.languages import CODE_LANGUAGES


class Role(enum.StrEnum):
    UI = "ui"
    API = "api"
    SERVICE = "service"
    DB = "db"
    TEST = "test"
    CONFIG = "config"
    DOCS = "docs"
    OTHER = "other"


UI_FRAMEWORKS = frozenset(
    {"react", "next", "vue", "nuxt", "svelte", "sveltekit", "angular", "astro", "solid-js"}
)

_TEST_DIRS = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "e2e", "cypress", "playwright", "__mocks__"}
)
_TEST_NAME = re.compile(
    r"^(?:test_.+\.py|.+_test\.(?:py|go|rb|exs?)|conftest\.py|.+\.(?:test|spec)\.[cm]?[jt]sx?"
    r"|.+_spec\.rb|.+(?:Test|Tests|IT)\.(?:java|kt|cs|swift))$"
)

_DOC_LANGUAGES = frozenset({"markdown", "mdx", "rst", "asciidoc"})
_DOC_DIRS = frozenset({"docs", "doc", "documentation"})
_DOC_NAMES = re.compile(
    r"^(?:readme|changelog|changes|history|contributing|license|licence|copying|notice|authors"
    r"|code_of_conduct|security|support|release-notes|releasing)(?:[.-].*)?$",
    re.IGNORECASE,
)

_CONFIG_LANGUAGES = frozenset(
    {
        "json",
        "yaml",
        "toml",
        "ini",
        "properties",
        "dotenv",
        "ignore",
        "gitattributes",
        "dockerfile",
        "makefile",
        "cmake",
        "hcl",
        "nix",
        "procfile",
        "just",
        "pip-requirements",
        "shell",
        "fish",
        "powershell",
        "batch",
    }
)
_CONFIG_DIRS = frozenset(
    {
        ".github",
        ".circleci",
        ".gitlab",
        ".husky",
        ".vscode",
        ".idea",
        ".devcontainer",
        "scripts",
        "settings",  # Django settings split into a package (settings/base.py, …)
    }
)
_CONFIG_NAME = re.compile(
    r"^(?:.+\.config\.[cm]?[jt]s|tsconfig.*\.json|setup\.py|setup\.cfg|manage\.py|noxfile\.py"
    r"|tasks\.py|fabfile\.py|gulpfile\.[cm]?js|gruntfile\.js|(?:config|settings|conf)\.py"
    r"|config\.[cm]?[jt]s)$",
    re.IGNORECASE,
)

_DB_LANGUAGES = frozenset({"sql", "prisma"})
_DB_DIRS = frozenset(
    {"migrations", "migration", "alembic", "models", "repositories", "repository", "db", "database"}
)
_DB_NAME = re.compile(
    r"^(?:models?\.py|db\.py|database\.py|crud\.py|repositor(?:y|ies)\.py|schema\.prisma"
    r"|.+\.(?:entity|model|repository|schema)\.[cm]?[jt]s)$"
)
_ORM_MODEL = re.compile(
    r"^\s*class\s+\w+\s*\([^)]*\b(?:table\s*=\s*True|DeclarativeBase|declarative_base|db\.Model"
    r"|models\.Model)\b"
    # JPA and Spring Data
    r"|^\s*@(?:Entity|Table|Document)\b"
    r"|\binterface\s+\w+\s+extends\s+(?:Jpa|Crud|PagingAndSorting|Mongo|Reactive\w*)Repository\b",
    re.MULTILINE,
)
_ORM_LANGUAGES = frozenset({"python", "java", "kotlin"})
# Spring's layers are packages, and a JVM file's package is its directory: the one it is in
# decides, not any above it (domain/service/PricingService.java is a service, the classes in
# domain/ entities). What the code says comes first (@Entity, @RestController, a repository
# interface); these catch the rest of a layer's package: embeddables, enums, DAOs on
# JdbcTemplate, exception handlers, filters.
_JVM_LANGUAGES = frozenset({"java", "kotlin"})
_JVM_DB_PACKAGES = frozenset({"entity", "entities", "model", "models", "domain", "dao", "daos"})
_JVM_API_PACKAGES = frozenset({"controller", "controllers", "web", "rest"})

# Folders that hold the data layer in one ecosystem only: EF Core's entities (TypeORM's are
# found by name), a Go service's store (a frontend's store/ is its state).
_LANGUAGE_DB_DIRS: dict[str, frozenset[str]] = {
    "csharp": frozenset({"entities"}),
    "go": frozenset({"store", "stores"}),
}

_API_DIRS = frozenset(
    {"api", "routes", "routers", "router", "controllers", "endpoints", "handlers"}
)
_API_NAME = re.compile(r"^(?:views\.py|urls\.py|.+\.(?:controller|resolver|routes?)\.[cm]?[jt]s)$")
_API_CODE = re.compile(
    # FastAPI/Flask/Starlette decorators, applications, routers and blueprints
    r"^\s*@\s*\w+\.(?:get|post|put|patch|delete|head|options|route|api_route|websocket)\s*\("
    r"|\b(?:APIRouter|Blueprint|FastAPI|Flask|Starlette)\s*\("
    # Express/Koa/Fastify-style registration
    r"|\b(?:app|router|server)\.(?:get|post|put|patch|delete|all)\s*\(\s*[\"'`]/"
    # NestJS
    r"|^\s*@(?:Controller|Get|Post|Put|Patch|Delete)\s*\("
    # Spring (Java, Kotlin)
    r"|^\s*@(?:RestController|Controller|RequestMapping|GetMapping|PostMapping|PutMapping"
    r"|PatchMapping|DeleteMapping)\b"
    # Go: net/http, gin, echo, chi, fiber
    r"|\bhttp\.Handle(?:Func)?\s*\("
    r"|\.(?:GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete|HandleFunc)\s*\(\s*\"/",
    re.MULTILINE,
)
# Frontend packages hold JavaScript and TypeScript; other languages there are not UI code.
_JS_LANGUAGES = frozenset({"javascript", "typescript", "jsx", "tsx"})
_FRONTEND_LANGUAGES = _JS_LANGUAGES | {"vue", "svelte", "astro"}
# Next.js route handlers and API routes (in a frontend package, but server endpoints).
# Next.js route handlers and API routes, SvelteKit endpoints (in a frontend package, but server
# endpoints).
_FRONTEND_HANDLER = re.compile(
    r"(?:^|/)(?:app/(?:.+/)?route\.[cm]?[jt]s|pages/api/.+\.[cm]?[jt]sx?|\+server\.[jt]s)$"
)

_UI_LANGUAGES = frozenset(
    {"vue", "svelte", "astro", "css", "scss", "sass", "less", "html", "jinja", "handlebars", "ejs"}
)
_JSX_RETURN = re.compile(r"(?:\breturn|=>)\s*\(?\s*<(?:[A-Za-z][\w.:-]*|>)")


@dataclass(frozen=True, slots=True)
class _File:
    path: str
    name: str
    directories: frozenset[str]
    language: str | None
    code: str
    ui_package: bool

    @property
    def jvm_package(self) -> str | None:
        """The directory a JVM file is in (its package's last part), lowercased."""
        if self.language not in _JVM_LANGUAGES:
            return None
        return PurePosixPath(self.path).parent.name.lower()


def _test(f: _File) -> bool:
    return bool(f.directories & _TEST_DIRS or _TEST_NAME.match(f.name))


def _docs(f: _File) -> bool:
    return bool(
        f.language in _DOC_LANGUAGES
        or f.directories & _DOC_DIRS
        or (_DOC_NAMES.match(f.name) and f.language not in CODE_LANGUAGES)
    )


def _config(f: _File) -> bool:
    return bool(
        f.language in _CONFIG_LANGUAGES
        or f.directories & _CONFIG_DIRS
        or _CONFIG_NAME.match(f.name)
        or (f.name.startswith(".") and f.language not in CODE_LANGUAGES)
    )


def _db(f: _File) -> bool:
    return bool(
        f.language in _DB_LANGUAGES
        or f.directories & _DB_DIRS
        or _DB_NAME.match(f.name)
        or (f.language in _ORM_LANGUAGES and _ORM_MODEL.search(f.code))
        or f.jvm_package in _JVM_DB_PACKAGES
        or f.directories & _LANGUAGE_DB_DIRS.get(f.language or "", frozenset())
    )


def _api(f: _File) -> bool:
    if _FRONTEND_HANDLER.search(f.path):
        return True
    if f.language in CODE_LANGUAGES and _API_CODE.search(f.code):
        return True
    if f.jvm_package in _JVM_API_PACKAGES:
        return True
    frontend = f.ui_package and f.language in _FRONTEND_LANGUAGES
    return not frontend and bool(f.directories & _API_DIRS or _API_NAME.match(f.name))


def _ui(f: _File) -> bool:
    if f.language in _UI_LANGUAGES:
        return True
    if f.language in ("tsx", "jsx") and _JSX_RETURN.search(f.code):
        return True
    return f.ui_package and f.language in _JS_LANGUAGES


def _service(f: _File) -> bool:
    return f.language in CODE_LANGUAGES


RULES: tuple[tuple[Role, Callable[[_File], bool]], ...] = (
    (Role.TEST, _test),
    (Role.DOCS, _docs),
    (Role.CONFIG, _config),
    (Role.DB, _db),
    (Role.API, _api),
    (Role.UI, _ui),
    (Role.SERVICE, _service),
)


def assign_role(path: str, language: str | None, text: str | None, *, ui_package: bool) -> Role:
    """``ui_package``: the file is in a frontend package (see the module docstring)."""
    pure = PurePosixPath(path)
    file = _File(
        path=path,
        name=pure.name,
        directories=frozenset(part.lower() for part in pure.parts[:-1]),
        language=language,
        code=text or "",
        ui_package=ui_package,
    )
    return next((role for role, matches in RULES if matches(file)), Role.OTHER)
