"""Which test framework a fix's test is written for, and how it runs (8.6).

From the repository's files, never running anything: the project is the nearest folder above
the culprit (the file the report points at) with a manifest (``pyproject.toml``, ``setup.cfg``,
``requirements*.txt`` or ``package.json``); its framework is pytest for Python, and vitest or
jest for JavaScript (as ``package.json`` names them). A reproducing test goes in a new file in
the project's test folder (``tests/``, ``test/``, ``__tests__/``; ``tests/`` when there is
none), named so the framework collects it and nobody mistakes it for the repository's own
(TypeScript where the project has a ``tsconfig.json``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from sensei_engine.snapshots import UnsafePathError, check_path, read_file

Framework = Literal["pytest", "vitest", "jest"]
PY_MANIFESTS = ("pyproject.toml", "setup.cfg", "setup.py", "requirements.txt")
TEST_DIRS = ("tests", "test", "__tests__")
MAX_MANIFEST = 2 * 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{6,32}$")


class NoFramework(Exception):
    """No test framework this fix can write a test for (what was looked for)."""


@dataclass(frozen=True, slots=True)
class TestSetup:
    framework: Framework
    project: str  # the project's folder in the repository ("" is the root)
    test_dir: str  # where the reproducing test goes, in the repository
    language: Literal["python", "javascript"]
    typescript: bool = False  # a JavaScript project with a tsconfig.json: the test is .ts

    def test_path(self, fix_id: str) -> str:
        """The reproducing test's file: new, the framework's naming, ours by its name."""
        if not _ID.match(fix_id):
            raise ValueError(f"not a fix id: {fix_id!r}")
        if self.framework == "pytest":
            return _join(self.test_dir, f"test_sensei_repro_{fix_id}.py")
        suffix = "ts" if self.typescript else "js"
        return _join(self.test_dir, f"sensei-repro-{fix_id}.test.{suffix}")

    def extra_path(self, fix_id: str) -> str:
        """The verifier's extra tests' file (8.7): beside the reproducing test, never kept."""
        repro = self.test_path(fix_id)
        return repro.replace("test_sensei_repro_", "test_sensei_extra_").replace(
            "sensei-repro-", "sensei-extra-"
        )

    def command(self, test_path: str) -> tuple[str, ...]:
        """The argv that runs only ``test_path``, from the project's folder."""
        relative = str(PurePosixPath(test_path).relative_to(self.project or "."))
        if self.framework == "pytest":
            return ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", relative)
        if self.framework == "vitest":
            return ("npx", "vitest", "run", relative)
        return ("npx", "jest", "--runTestsByPath", relative)

    def suite(self) -> tuple[str, ...]:
        """The argv that runs the project's whole suite (8.7), saying which tests failed
        (:mod:`sensei_fixer.suites` reads it)."""
        if self.framework == "pytest":
            return ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE",
                    "--tb=short")  # fmt: skip
        if self.framework == "vitest":
            return ("npx", "vitest", "run")
        return ("npx", "jest", "--ci")


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _read(root: Path, path: str) -> bytes | None:
    try:
        return read_file(root, path, max_bytes=MAX_MANIFEST)
    except (OSError, ValueError):
        return None


def _exists(root: Path, path: str) -> bool:
    try:
        target = root / check_path(path)
    except UnsafePathError:
        return False
    return target.exists() and not target.is_symlink()


def _is_dir(root: Path, path: str) -> bool:
    try:
        target = root / check_path(path)
    except UnsafePathError:
        return False
    return target.is_dir() and not target.is_symlink()


def _js_framework(package: bytes) -> Framework | None:
    try:
        found = json.loads(package)
    except ValueError:
        return None
    if not isinstance(found, dict):
        return None
    spec = cast("dict[str, Any]", found)
    deps: set[str] = set()
    for key in ("devDependencies", "dependencies"):
        value = spec.get(key)
        if isinstance(value, dict):
            deps |= set(cast("dict[str, Any]", value))
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps:
        return "jest"
    return None


def _project_of(root: Path, culprit: str | None) -> list[str]:
    """The folders from the culprit's up to the root, nearest first."""
    if not culprit:
        return [""]
    parts = PurePosixPath(culprit).parts[:-1]
    return ["/".join(parts[:n]) for n in range(len(parts), -1, -1)]


def detect(root: Path, culprit: str | None) -> TestSetup:
    """The test setup for a fix whose culprit is ``culprit`` (a path in the checkout at
    ``root``; None: the repository's root project)."""
    language = None
    if culprit is not None:
        suffix = PurePosixPath(culprit).suffix
        language = "python" if suffix == ".py" else "javascript" if suffix in (
            ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"
        ) else None  # fmt: skip
    for folder in _project_of(root, culprit):
        if language in (None, "javascript"):
            package = _read(root, _join(folder, "package.json"))
            if package is not None:
                framework = _js_framework(package)
                if framework is not None:
                    typescript = _exists(root, _join(folder, "tsconfig.json"))
                    return TestSetup(framework, folder, _test_dir(root, folder), "javascript",
                                     typescript)  # fmt: skip
        if language in (None, "python") and any(
            _exists(root, _join(folder, name)) for name in PY_MANIFESTS
        ):
            return TestSetup("pytest", folder, _test_dir(root, folder), "python")
    raise NoFramework(
        "No pytest, vitest or jest project was found around "
        f"{culprit or 'the repository root'}: a reproducing test can't be written for it."
    )


def _test_dir(root: Path, project: str) -> str:
    for name in TEST_DIRS:
        folder = _join(project, name)
        if _is_dir(root, folder):
            return folder
    return _join(project, "tests")
