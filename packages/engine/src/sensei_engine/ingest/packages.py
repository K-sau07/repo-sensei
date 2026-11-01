"""Third-party packages (6.5): what a repository declares it depends on, and at which version.

Declared, per manifest: ``pyproject.toml`` (PEP 621 dependencies and optional ones, PEP 735
groups, Poetry's, uv's dev dependencies), ``requirements*.txt`` and ``package.json`` (its
dependencies, peer and optional ones, and dev ones). Resolved, from the lockfile at the manifest's
folder or above it: ``uv.lock``, ``poetry.lock``, ``package-lock.json`` and ``pnpm-lock.yaml``
(its importers, for a workspace). A package is named as its registry names it (Python names
normalized, PEP 503); ``spec`` is the constraint as written, ``version`` the locked one.
"""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from sensei_core.logging import get_logger

log = get_logger(__name__)

Ecosystem = Literal["pypi", "npm"]
LOCKFILES = frozenset({"uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml"})
MAX_LOCK_BYTES = 32 << 20  # a lockfile read for its versions
TOP = "."
_REQUIREMENTS = re.compile(r"^requirements[\w.-]*\.(?:txt|in)$")
_DEV_WORDS = re.compile(r"(^|[^a-z])(dev|test|tests|lint|docs?|typing)([^a-z]|$)")


@dataclass(frozen=True, slots=True)
class Package:
    ecosystem: Ecosystem
    name: str
    spec: str | None  # the constraint as written ("^19.1.0", ">=0.115,<1")
    version: str | None  # the lockfile's
    manifest: str  # where it is declared
    root: str  # that manifest's folder
    dev: bool  # a development dependency (dev, test, lint, docs groups)

    def as_json(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "name": self.name,
            "spec": self.spec,
            "version": self.version,
            "manifest": self.manifest,
            "root": self.root,
            "dev": self.dev,
        }


def is_lockfile(path: str) -> bool:
    return PurePosixPath(path).name in LOCKFILES


def is_declaring(path: str) -> bool:
    """A manifest that declares packages."""
    pure = PurePosixPath(path)
    return pure.name in ("pyproject.toml", "package.json") or _is_requirements(pure)


def _is_requirements(pure: PurePosixPath) -> bool:
    return bool(_REQUIREMENTS.match(pure.name)) or (
        pure.parent.name == "requirements" and pure.suffix in (".txt", ".in")
    )


def _folder(path: str) -> str:
    pure = PurePosixPath(path)
    parent = pure.parent
    if parent.name == "requirements" and pure.name != "requirements.txt":
        parent = parent.parent
    return str(parent) if str(parent) not in ("", ".") else TOP


def _table(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    return (
        [v for v in cast("list[object]", value) if isinstance(v, str)]
        if isinstance(value, list)
        else []
    )


def _pep508(line: str) -> tuple[str, str | None] | None:
    try:
        found = Requirement(line)
    except InvalidRequirement:
        return None
    return canonicalize_name(found.name), str(found.specifier) or None


# ---------------------------------------------------------------- declared


def _pyproject(text: str) -> Iterator[tuple[str, str | None, bool]]:
    data = tomllib.loads(text)
    project = _table(data.get("project"))
    for line in _strings(project.get("dependencies")):
        if (found := _pep508(line)) is not None:
            yield (*found, False)
    for group, lines in _table(project.get("optional-dependencies")).items():
        for line in _strings(lines):
            if (found := _pep508(line)) is not None:
                yield (*found, bool(_DEV_WORDS.search(group.lower())))
    for _, lines in _table(data.get("dependency-groups")).items():
        for line in _strings(lines):
            if (found := _pep508(line)) is not None:
                yield (*found, True)
    tool = _table(data.get("tool"))
    for line in _strings(_table(tool.get("uv")).get("dev-dependencies")):
        if (found := _pep508(line)) is not None:
            yield (*found, True)
    poetry = _table(tool.get("poetry"))
    yield from _poetry(_table(poetry.get("dependencies")), dev=False)
    yield from _poetry(_table(poetry.get("dev-dependencies")), dev=True)
    for group, held in _table(poetry.get("group")).items():
        yield from _poetry(_table(_table(held).get("dependencies")), dev=group != "main")


def _poetry(table: Mapping[str, Any], *, dev: bool) -> Iterator[tuple[str, str | None, bool]]:
    for name, spec in table.items():
        if name.lower() == "python":
            continue
        version = spec if isinstance(spec, str) else _table(spec).get("version")
        yield canonicalize_name(name), version if isinstance(version, str) else None, dev


def _requirements(path: str, text: str) -> Iterator[tuple[str, str | None, bool]]:
    dev = bool(_DEV_WORDS.search(PurePosixPath(path).name.lower()))
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        if (found := _pep508(line)) is not None:
            yield (*found, dev)


def _package_json(text: str) -> Iterator[tuple[str, str | None, bool]]:
    data = _table(json.loads(text))
    for key, dev in (
        ("dependencies", False), ("peerDependencies", False), ("optionalDependencies", False),
        ("devDependencies", True),
    ):  # fmt: skip
        for name, spec in _table(data.get(key)).items():
            yield name, spec if isinstance(spec, str) else None, dev


def _declared(path: str, text: str) -> Iterator[tuple[Ecosystem, str, str | None, bool]]:
    name = PurePosixPath(path).name
    if name == "package.json":
        for package, spec, dev in _package_json(text):
            yield "npm", package, spec, dev
    elif name == "pyproject.toml":
        for package, spec, dev in _pyproject(text):
            yield "pypi", package, spec, dev
    else:
        for package, spec, dev in _requirements(path, text):
            yield "pypi", package, spec, dev


# ---------------------------------------------------------------- resolved


Locked = dict[tuple[Ecosystem, str], dict[str, str]]  # (ecosystem, folder) → name → version


def _locked(path: str, text: str, into: Locked) -> None:
    name = PurePosixPath(path).name
    folder = _folder(path)
    if name in ("uv.lock", "poetry.lock"):
        _python_lock(text, into.setdefault(("pypi", folder), {}))
    elif name == "package-lock.json":
        _npm_lock(text, into.setdefault(("npm", folder), {}))
    elif name == "pnpm-lock.yaml":
        _pnpm_lock(text, folder, into)


def _python_lock(text: str, held: dict[str, str]) -> None:
    for item in cast("list[object]", tomllib.loads(text).get("package") or []):
        entry = _table(item)
        name, version = entry.get("name"), entry.get("version")
        if isinstance(name, str) and isinstance(version, str):
            held.setdefault(canonicalize_name(name), version)


def _npm_lock(text: str, held: dict[str, str]) -> None:
    data = _table(json.loads(text))
    for key, entry in _table(data.get("packages")).items():
        # Top-level installs only: "node_modules/x" (a workspace's own are under its folder).
        if key.startswith("node_modules/") and "/node_modules/" not in key:
            version = _table(entry).get("version")
            if isinstance(version, str):
                held.setdefault(key.removeprefix("node_modules/"), version)
    for package, entry in _table(data.get("dependencies")).items():  # lockfile v1
        version = _table(entry).get("version")
        if isinstance(version, str):
            held.setdefault(package, version)


def _pnpm_lock(text: str, folder: str, into: Locked) -> None:
    data = _table(yaml.safe_load(text))
    importers = _table(data.get("importers")) or {TOP: data}
    for importer, entry in importers.items():
        at = str(posixpath.normpath(posixpath.join("" if folder == TOP else folder, importer)))
        held = into.setdefault(("npm", TOP if at in (".", "") else at), {})
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            for package, spec in _table(_table(entry).get(key)).items():
                version = spec if isinstance(spec, str) else _table(spec).get("version")
                if isinstance(version, str):
                    held.setdefault(package, version.split("(", 1)[0])


def _version(locked: Locked, ecosystem: Ecosystem, root: str, name: str) -> str | None:
    """The locked version: the manifest's folder's lockfile, else the nearest above it."""
    folder: str = root
    while True:
        found = locked.get((ecosystem, folder), {}).get(name)
        if found is not None:
            return found
        if folder == TOP:
            return None
        folder = str(posixpath.dirname(folder)) or TOP


def inventory(manifests: Mapping[str, str], lockfiles: Mapping[str, str]) -> list[Package]:
    """Every declared package (see the module), by manifest, then ecosystem and name; a
    manifest or lockfile that can't be read is skipped (and logged)."""
    locked: Locked = {}
    for path in sorted(lockfiles):
        try:
            _locked(path, lockfiles[path], locked)
        except Exception:  # any malformed lockfile: its versions are unknown, nothing else
            log.warning("lockfile skipped", path=path, exc_info=True)
    found: dict[tuple[str, Ecosystem, str], Package] = {}
    for path in sorted(manifests):
        if not is_declaring(path):
            continue
        root = _folder(path)
        try:
            declared = list(_declared(path, manifests[path]))
        except Exception:  # any malformed manifest
            log.warning("manifest skipped", path=path, exc_info=True)
            continue
        for ecosystem, name, spec, dev in declared:
            key = (path, ecosystem, name)
            held = found.get(key)
            if held is not None and not held.dev:
                continue  # declared for running already: that counts
            found[key] = Package(
                ecosystem, name, spec, _version(locked, ecosystem, root, name), path, root, dev
            )
    return sorted(found.values(), key=lambda p: (p.manifest, p.ecosystem, p.name))


def find_lockfiles(root: Any, manifests: Iterable[str]) -> list[str]:
    """The lockfiles beside the declaring manifests and in the folders above them, up to the
    repository's root (``root`` a :class:`~pathlib.Path`). The walk leaves lockfiles out (they
    aren't code to index), so they are looked for where they would be; a symlink isn't read."""
    folders: set[str] = set()
    for path in manifests:
        if not is_declaring(path):
            continue
        folder = _folder(path)
        while True:
            folders.add(folder)
            if folder == TOP:
                break
            folder = str(posixpath.dirname(folder)) or TOP
    found: list[str] = []
    for folder in sorted(folders):
        for name in sorted(LOCKFILES):
            path = name if folder == TOP else f"{folder}/{name}"
            target = root / path
            if target.is_file() and not target.is_symlink():
                found.append(path)
    return found


def read_lockfiles(root: Any, paths: Iterable[str]) -> dict[str, str]:
    """The lockfiles' text (``root`` a :class:`~pathlib.Path`), each up to
    :data:`MAX_LOCK_BYTES` (a bigger one is left out: its versions unknown)."""
    texts: dict[str, str] = {}
    for path in paths:
        target = root / path
        try:
            if target.stat().st_size > MAX_LOCK_BYTES:
                continue
            texts[path] = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return texts
