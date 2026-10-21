"""Discovery on a real monorepo: fastapi/full-stack-fastapi-template at a pinned commit (see
fixtures/README.md). The expected numbers were checked file by file when the fixture was added;
a heuristic change that moves them should be a deliberate one."""

from __future__ import annotations

import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from sensei_engine.ingest import discover as discover_module
from sensei_engine.ingest.discover import DiscoveredFile, Discovery, discover
from sensei_engine.ingest.roles import Role

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"


@pytest.fixture(scope="module")
def found(tmp_path_factory: pytest.TempPathFactory) -> Discovery:
    root = tmp_path_factory.mktemp("fixture")
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(root, filter="data")
    return discover(root / "fastapi-template", max_files=10_000)


def _roles(found: Discovery) -> dict[str, Role]:
    return {file.path: file.role for file in found.files}


def test_exact_counts_per_role(found: Discovery) -> None:
    summary = found.summary()
    assert {role: totals["files"] for role, totals in summary["roles"].items()} == {
        "api": 10,
        "config": 67,
        "db": 10,
        "docs": 12,
        "other": 14,
        "service": 6,
        "test": 27,
        "ui": 99,
    }
    assert (summary["files"], summary["loc"]) == (245, 18567)


def test_counts_per_language(found: Discovery) -> None:
    languages = {lang: totals["files"] for lang, totals in found.summary()["languages"].items()}
    assert languages == {
        "css": 1,
        "dockerfile": 2,
        "dotenv": 2,
        "gitattributes": 1,
        "html": 4,
        "ignore": 6,
        "ini": 1,
        "json": 13,
        "mako": 1,
        "markdown": 10,
        "python": 43,
        "shell": 8,
        "svg": 5,
        "text": 3,
        "toml": 2,
        "tsx": 70,
        "typescript": 39,
        "unknown": 11,
        "yaml": 23,
    }


def test_languages_match_github_where_both_count_the_same_files(found: Discovery) -> None:
    languages = found.github_languages()
    # GitHub's numbers for this commit: Python 90980, HTML 21286, CSS 4187, Shell 1609, Mako 523.
    # TypeScript differs on purpose: GitHub counts the generated API client (87,740 bytes).
    assert {k: languages[k] for k in ("Python", "HTML", "CSS", "Shell", "Mako")} == {
        "Python": 90980,
        "HTML": 21286,
        "CSS": 4187,
        "Shell": 1609,
        "Mako": 523,
    }
    assert languages["TypeScript"] == 317948 - 87740


def test_ignore_rules_and_symlinks(found: Discovery) -> None:
    paths = {file.path for file in found.files}
    # Root .gitignore: ".vscode/*" then "!.vscode/extensions.json". Lockfiles: built-in deny.
    assert ".vscode/extensions.json" in paths
    assert not paths & {".vscode/launch.json", "uv.lock", "bun.lock"}
    assert found.summary()["skipped"] == {
        "ignored_files": 3,
        "ignored_dirs": 0,
        "symlinks": 4,  # .agents/skills/* and .claude/skills/* point into .venv
        "special": 0,
        "bad_names": 0,
    }
    assert not any(
        path.startswith((".agents/skills/fastapi", ".claude/skills/sqlmodel")) for path in paths
    )


def test_generated_and_binary_files(found: Discovery) -> None:
    generated = sorted(file.path for file in found.files if "generated" in file.flags)
    assert len(generated) == 17
    assert all(path.startswith("frontend/src/") for path in generated)
    assert "frontend/src/routeTree.gen.ts" in generated
    assert "frontend/src/client/index.ts" in generated  # by its header comment, not its name
    binary = sorted(file.path for file in found.files if "binary" in file.flags)
    assert binary == [
        "frontend/public/assets/images/favicon.png",
        *(
            f"img/{name}.png"
            for name in (
                "dashboard-dark",
                "dashboard-items",
                "dashboard",
                "docs",
                "github-social-preview",
                "login",
                "mailpit",
                "react-email",
            )
        ),
    ]


def test_frameworks_per_package(found: Discovery) -> None:
    assert [(f.root, f.name) for f in found.frameworks] == [
        (".", "docker-compose"),
        ("backend", "alembic"),
        ("backend", "fastapi"),
        ("backend", "httpx"),
        ("backend", "pydantic"),
        ("backend", "pytest"),
        ("backend", "sqlmodel"),
        ("frontend", "@tanstack/react-query"),
        ("frontend", "@tanstack/react-router"),
        ("frontend", "axios"),
        ("frontend", "playwright"),
        ("frontend", "react"),
        ("frontend", "vite"),
        ("packages/react-email", "react"),
    ]
    compose = found.frameworks[0]
    assert compose.details["services"] == [
        "adminer",
        "backend",
        "db",
        "mailpit",
        "playwright",
        "proxy",
    ]
    fastapi = next(f for f in found.frameworks if f.name == "fastapi")
    assert fastapi.version == ">=0.141.1,<1.0.0"


def test_roles_of_landmark_files(found: Discovery) -> None:
    roles = _roles(found)
    assert roles["backend/app/api/routes/items.py"] is Role.API
    assert roles["backend/app/main.py"] is Role.API
    assert roles["backend/app/models.py"] is Role.DB
    assert roles["backend/app/alembic/versions/e2412789c190_initialize_models.py"] is Role.DB
    assert roles["backend/app/core/security.py"] is Role.SERVICE
    assert roles["backend/app/core/config.py"] is Role.CONFIG
    assert roles["backend/tests/api/routes/test_items.py"] is Role.TEST
    # TanStack Router's routes are pages, not API routes: the frontend package decides.
    assert roles["frontend/src/routes/_layout/items.tsx"] is Role.UI
    assert roles["frontend/src/hooks/useAuth.ts"] is Role.UI
    assert roles["frontend/tests/items.spec.ts"] is Role.TEST
    assert roles["packages/react-email/emails/new_account.tsx"] is Role.UI


def test_framework_roots_on_files(found: Discovery) -> None:
    roots = {file.path: file.framework_root for file in found.files}
    assert roots["backend/app/main.py"] == "backend"
    assert roots["frontend/src/main.tsx"] == "frontend"
    assert roots["packages/react-email/ui/Button.tsx"] == "packages/react-email"
    assert roots["README.md"] == "."


def test_discovery_is_deterministic(found: Discovery, tmp_path: Path) -> None:
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    again = discover(tmp_path / "fastapi-template", max_files=10_000)
    assert again.files == found.files
    assert again.summary() == found.summary()


def test_the_walk_and_each_batch_are_reported(
    found: Discovery, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discover_module, "PROGRESS_EVERY", 100)
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    order: list[str] = []
    listed: list[str] = []
    reports: list[tuple[int, int]] = []
    batches: list[DiscoveredFile] = []

    def walked(paths: Sequence[str]) -> None:
        order.append("walked")
        listed.extend(paths)

    def progress(done: int, total: int, batch: Sequence[DiscoveredFile]) -> None:
        order.append("progress")
        reports.append((done, total))
        batches.extend(batch)

    again = discover(
        tmp_path / "fastapi-template", max_files=10_000, walked=walked, progress=progress
    )
    # The whole list first (before any file is read), then batches that add up to every file.
    assert order == ["walked", "progress", "progress", "progress"]
    assert listed == [file.path for file in found.files]
    assert reports == [(100, 245), (200, 245), (245, 245)]
    assert batches == again.files
    # Every file has its own row id, and the batches carry the same ones.
    assert len({file.id for file in again.files}) == 245
    assert [file.id for file in batches] == [file.id for file in again.files]
