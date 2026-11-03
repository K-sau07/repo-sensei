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
import tomllib
from collections.abc import Iterable
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
        (:mod:`sensei_fixer.suites` reads it), as the project configures it (a patch's own new
        tests are moved aside while it runs: :func:`sensei_fixer.patch.run_suite`)."""
        if self.framework == "pytest":
            return ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA",
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


def _real(root: Path, path: str) -> Path | None:
    """``path`` under ``root``, if nothing on the way is a symbolic link (a repository's link
    could point a test, written there, outside the checkout)."""
    try:
        target = root / check_path(path)
    except UnsafePathError:
        return None
    for step in [target, *target.parents]:
        if step == root:
            return target
        if step.is_symlink():
            return None
    return None


def _exists(root: Path, path: str) -> bool:
    target = _real(root, path)
    return target is not None and target.exists()


def _is_dir(root: Path, path: str) -> bool:
    target = _real(root, path)
    return target is not None and target.is_dir()


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


_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _names(specs: Iterable[object]) -> set[str]:
    """Distribution names in requirement strings, as modules would be named (``PyYAML`` and
    ``python-dateutil`` stay as they are: their import names differ, and aren't guessed)."""
    found: set[str] = set()
    for spec in specs:
        match = _REQUIREMENT.match(str(spec)) if isinstance(spec, str) else None
        if match:
            found.add(match.group(1).lower().replace("-", "_").replace(".", "_"))
    return found


def dependencies(root: Path, project: str) -> frozenset[str]:
    """The Python project's dependencies' names (its ``pyproject.toml`` and ``requirements*.txt``,
    and the repository root's): a new module named like one would be imported instead of it."""
    found: set[str] = set()
    for folder in dict.fromkeys((project, "")):
        raw = _read(root, _join(folder, "pyproject.toml"))
        if raw is not None:
            try:
                spec = tomllib.loads(raw.decode("utf-8", "replace"))
            except tomllib.TOMLDecodeError:
                spec = {}
            table = cast("dict[str, Any]", spec.get("project") or {})
            found |= _names(cast("list[object]", table.get("dependencies") or []))
            for extra in cast("dict[str, Any]", table.get("optional-dependencies") or {}).values():
                found |= _names(cast("list[object]", extra or []))
            poetry = cast("dict[str, Any]", cast("dict[str, Any]", spec.get("tool") or {}).get(
                "poetry") or {})  # fmt: skip
            found |= _names(list(cast("dict[str, Any]", poetry.get("dependencies") or {})))
        for name in ("requirements.txt", "requirements-dev.txt", "requirements/base.txt"):
            text = _read(root, _join(folder, name))
            if text is not None:
                found |= _names(text.decode("utf-8", "replace").splitlines())
    found.discard("python")
    return frozenset(found)


def _test_dir(root: Path, project: str) -> str:
    for name in TEST_DIRS:
        folder = _join(project, name)
        if _is_dir(root, folder):
            return folder
    return _join(project, "tests")
