"""A repository's Python dependencies, installed into an image its sandboxed runs start from (3.7).

:func:`detect` reads the project's manifests from the unpacked snapshot (never running anything):

- ``uv.lock`` (the project's, or its workspace's in a directory above): what the project and the
  workspace members it uses require, with every version the lock pins as a constraint;
- otherwise ``pyproject.toml``'s ``[project].dependencies`` and every ``requirements*.txt``.

Only plain requirements from the package index are kept: options (``-e``, ``-r``,
``--index-url``...), direct URLs, VCS and local paths are skipped and listed in ``skipped``. The
project itself isn't installed: its code is the snapshot at ``/src``.

:meth:`DependencyImages.prepare` builds ``FROM`` the runner image with ``uv pip install`` from
one index (PyPI unless set otherwise; the repository's own index settings are ignored). It tries
wheels only first (``--no-build``), so for most repositories no code from a package runs while
the build has network; packages that only ship sources are built from source on a second try,
unless that's turned off. The image is tagged by a hash of what it installs and the runner
image's id, so the same lockfile reuses it, and any change makes a new one. Anything that keeps
the dependencies from installing (no wheel, a version that doesn't exist, the time limit) is a
limitation, not an error: the run gets the bare runner image, and the caller falls back to
static analysis.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
import tomllib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import orjson
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from sensei_core.config import SandboxSettings
from sensei_core.redaction import redact_text
from sensei_engine.sandbox.runner import (
    DEFAULT_PYTHON,
    LABEL,
    Sandbox,
    SandboxError,
    SandboxUnavailableError,
    docker_env,
)
from sensei_engine.snapshots import UnsafePathError, check_path, read_file

PYTHONS = ("3.13", "3.14", "3.12")  # runner images, the preferred one first
INDEX_URL = "https://pypi.org/simple"
DEPS_IMAGE = "repo-sensei/sandbox-deps"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_LOCK_BYTES = 32 * 1024 * 1024
MAX_REQUIREMENT_FILES = 16
MAX_REQUIREMENTS = 2000
INSTALL_TIMEOUT_SECONDS = 600.0
LOG_TAIL_BYTES = 4096  # of the install's output, kept as the limitation's detail
MAX_DETAIL_LINES = 40

Source = Literal["uv.lock", "pyproject", "requirements"]

_REQUIREMENTS_FILE_RE = re.compile(r"requirements[\w.-]*\.txt")
_LOCAL_SOURCES = ("editable", "virtual", "directory", "path")
_DIRECT_SOURCES = (*_LOCAL_SOURCES, "git", "url")
# docker build --progress=plain: "#7 0.710 <what the step printed>".
_STEP_LINE_RE = re.compile(r"#\d+ \d+\.\d+ (.*)")
_NO_WHEELS = "building from source is disabled"  # uv's hint when --no-build left no candidates
_EXTRA_RE = re.compile(r"""\bextra\s*==\s*['"]([^'"]+)['"]""")
# A distribution's name (PEP 508's, which PEP 503 normalizes): nothing else goes in a pin.
_NAME_RE = re.compile(r"[A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Dependencies:
    project: str  # the directory in the snapshot the manifests are in ("" is the root)
    sources: tuple[str, ...]  # the manifests read, relative to the snapshot
    python: str  # the runner image's Python version
    requirements: tuple[str, ...]  # PEP 508, sorted
    constraints: tuple[str, ...] = ()  # name==version pins (from the lock)
    skipped: tuple[str, ...] = ()  # what isn't installed, and why

    @property
    def empty(self) -> bool:
        return not self.requirements

    def key(self, base_image_id: str, recipe: str) -> str:
        """What the image installs, how, and onto what: equal keys are the same image."""
        what = [base_image_id, recipe, self.python, self.requirements, self.constraints]
        return hashlib.sha256(orjson.dumps(what)).hexdigest()


@dataclass(frozen=True, slots=True)
class Prepared:
    """The image a run starts from, and whether the repository's dependencies are in it."""

    image: str
    dependencies: Dependencies
    installed: bool
    limitation: str | None = None  # why they aren't (then ``image`` is the bare runner)
    detail: str = ""  # the end of the install's output, redacted
    built: bool = False  # this call built it (else it was there already)
    sdists: bool = False  # some packages were built from source


@dataclass(slots=True)
class _Collected:
    requirements: set[str] = field(default_factory=set[str])
    skipped: list[str] = field(default_factory=list[str])

    def add(self, line: str, where: str) -> None:
        """One requirement line, kept if it's a plain one from the index."""
        try:
            req = Requirement(line)
        except InvalidRequirement:
            self.skipped.append(f"{where}: not a requirement: {line[:200]}")
            return
        if req.url:
            self.skipped.append(f"{where}: {req.name} comes from a URL, not the index")
            return
        self.requirements.add(str(req))


def _read(root: Path, path: str, max_bytes: int) -> bytes | None:
    """A file in the snapshot, or None when it isn't there (or is a link, or too big)."""
    try:
        data = read_file(root, path, max_bytes=max_bytes + 1)
    except (OSError, UnsafePathError):
        return None
    return data if len(data) <= max_bytes else None


def _toml(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _table(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _tables(value: object) -> list[dict[str, Any]]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [cast("dict[str, Any]", item) for item in items if isinstance(item, dict)]


def _strings(value: object) -> list[str]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [str(item) for item in items]


def _join(project: str, name: str) -> str:
    return f"{project}/{name}" if project else name


def _python(spec: object) -> tuple[str, str | None]:
    """The runner's Python for a ``requires-python`` (the first of ``PYTHONS`` it allows), and a
    note when it allows none of them."""
    if not isinstance(spec, str):
        return DEFAULT_PYTHON, None
    try:
        allowed = SpecifierSet(spec)
    except InvalidSpecifier:
        return DEFAULT_PYTHON, f"requires-python isn't a version range: {spec[:100]}"
    for version in PYTHONS:
        if allowed.contains(version, prereleases=True):
            return version, None
    return DEFAULT_PYTHON, f"requires-python {spec[:100]} allows none of {', '.join(PYTHONS)}"


def requirement_lines(text: str) -> list[str]:
    """requirements.txt lines as requirements: comments, options and hashes dropped,
    continuation lines joined. Option lines come back as they are (starting with "-")."""
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if not line:
            continue
        if not line.startswith("-"):
            line = re.split(r"\s+--?\w", line, maxsplit=1)[0].strip()  # per-line --hash=...
        lines.append(line)
    return lines


def _lock_requirement(entry: dict[str, Any]) -> str:
    name = str(entry.get("name", ""))
    extras = _strings(entry.get("extras"))
    text = name + (f"[{','.join(sorted(extras))}]" if extras else "")
    text += str(entry.get("specifier") or "")
    marker = entry.get("marker")
    return f"{text} ; {marker}" if marker else text


def _pin(name: str, version: str) -> str | None:
    """``name==version`` from the lock's strings, or None unless both are what they claim: the
    lock is the repository's, and a line in a constraints file can be an option
    (``--extra-index-url``) or a URL as much as a pin."""
    if not _NAME_RE.fullmatch(name):  # all of it: `$` would let a trailing newline by
        return None
    try:
        parsed = Version(version)
    except InvalidVersion:
        return None
    return f"{name}=={parsed}"


def _pins(pinned: dict[str, set[str]], collected: _Collected, where: str) -> tuple[str, ...]:
    """The constraints: each package the lock pins to one version (several, one per Python,
    say, are left to the resolver), as long as it reads as a pin."""
    pins: list[str] = []
    for name, versions in sorted(pinned.items()):
        if len(versions) != 1:
            continue
        version = next(iter(versions))
        pin = _pin(name, version)
        if pin is None:
            collected.skipped.append(f"{where}: not a pin: {name[:100]!r} {version[:100]!r}")
        else:
            pins.append(pin)
    return tuple(pins)


def _from_lock(
    lock: dict[str, Any], collected: _Collected, where: str, member: str
) -> tuple[str, ...]:
    """What the lock's project in ``member`` (its directory relative to the lock's, "." for the
    lock's own) requires from the index, its workspace members followed (all of its local
    projects when none is there); returns the versions the lock pins (one per package name)."""
    local: dict[str, dict[str, Any]] = {}
    pinned: dict[str, set[str]] = {}
    for package in _tables(lock.get("package")):
        name, source = str(package.get("name", "")), _table(package.get("source"))
        if any(key in source for key in _LOCAL_SOURCES):
            local[name] = package
        elif "registry" in source and package.get("version"):
            pinned.setdefault(name, set()).add(str(package["version"]))
    roots = [
        name
        for name, package in local.items()
        if member in [str(v).rstrip("/") for v in _table(package.get("source")).values()]
    ] or list(local)
    queue = deque((name, frozenset[str]()) for name in roots)
    seen: set[tuple[str, frozenset[str]]] = set()
    while queue:
        name, extras = queue.popleft()
        if (name, extras) in seen or name not in local:
            continue
        seen.add((name, extras))
        for entry in _tables(_table(local[name].get("metadata")).get("requires-dist")):
            marker = str(entry.get("marker") or "")
            wanted = set(_EXTRA_RE.findall(marker))
            if wanted and not wanted & extras:
                continue  # an extra nobody asked for
            # Asked for: the extra clause is satisfied (any other clause in it is dropped too).
            req = {**entry, "marker": None} if wanted else entry
            dep = str(req.get("name", ""))
            if dep in local:
                queue.append((dep, frozenset(_strings(req.get("extras")))))
            elif any(key in req for key in _DIRECT_SOURCES):
                collected.skipped.append(f"{where}: {dep} isn't from the index")
            else:
                collected.add(_lock_requirement(req), where)
    return _pins(pinned, collected, where)


def _requirement_files(root: Path, project: str) -> list[str]:
    directory = root / project if project else root
    if directory.is_symlink() or not directory.is_dir():
        return []
    with contextlib.suppress(OSError), os.scandir(directory) as entries:
        names = sorted(
            entry.name
            for entry in entries
            if _REQUIREMENTS_FILE_RE.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
        )
        return names[:MAX_REQUIREMENT_FILES]
    return []


def _from_manifests(
    root: Path, project: str, pyproject: dict[str, Any] | None, collected: _Collected
) -> list[str]:
    """``[project].dependencies`` and the requirements files; returns the files read."""
    sources: list[str] = []
    if pyproject is not None:
        path = _join(project, "pyproject.toml")
        deps = _strings(_table(pyproject.get("project")).get("dependencies"))
        if deps:
            sources.append(path)
        for line in deps:
            collected.add(line, path)
    for name in _requirement_files(root, project):
        path = _join(project, name)
        data = _read(root, path, MAX_MANIFEST_BYTES)
        if data is None:
            continue
        sources.append(path)
        for line in requirement_lines(data.decode("utf-8", "replace")):
            if line.startswith("-"):
                collected.skipped.append(f"{path}: option not followed: {line[:200]}")
            else:
                collected.add(line, path)
    return sources


def _find_lock(root: Path, project: str) -> tuple[str, dict[str, Any] | None, str]:
    """The ``uv.lock`` of ``project``: its own, or a workspace's in a directory above it (with
    a ``pyproject.toml`` beside it). Returns its path, the lock, and the project's directory
    relative to the lock's ("." when it's the same)."""
    parts = project.split("/") if project else []
    for depth in range(len(parts), -1, -1):
        directory = "/".join(parts[:depth])
        path = _join(directory, "uv.lock")
        lock = _toml(_read(root, path, MAX_LOCK_BYTES))
        if lock is not None and (depth == len(parts) or _exists(root, directory)):
            return path, lock, "/".join(parts[depth:]) or "."
    return "", None, ""


def _exists(root: Path, directory: str) -> bool:
    return _read(root, _join(directory, "pyproject.toml"), MAX_MANIFEST_BYTES) is not None


def detect(root: Path, project: str = "") -> Dependencies:
    """The dependencies of the Python project in ``project`` (a directory in the snapshot
    unpacked at ``root``), from its manifests; empty when it has none."""
    try:
        project = check_path(project)
    except UnsafePathError as exc:
        raise SandboxError(f"not a directory in the snapshot: {project!r}") from exc
    collected = _Collected()
    pyproject_path = _join(project, "pyproject.toml")
    pyproject = _toml(_read(root, pyproject_path, MAX_MANIFEST_BYTES))
    lock_path, lock, member = _find_lock(root, project) if pyproject is not None else ("", None, "")
    if lock is not None:  # the lock's range covers every workspace member's
        spec, where = lock.get("requires-python"), lock_path
        sources, constraints = [lock_path], _from_lock(lock, collected, lock_path, member)
    else:
        spec, where = (
            _table((pyproject or {}).get("project")).get("requires-python"),
            pyproject_path,
        )
        sources, constraints = _from_manifests(root, project, pyproject, collected), ()
    python, note = _python(spec)
    if note:
        collected.skipped.append(f"{where}: {note}")
    requirements = sorted(collected.requirements)
    if len(requirements) > MAX_REQUIREMENTS:
        collected.skipped.append(f"more than {MAX_REQUIREMENTS} requirements: the rest dropped")
        requirements = requirements[:MAX_REQUIREMENTS]
    return Dependencies(
        project=project,
        sources=tuple(sources),
        python=python,
        requirements=tuple(requirements),
        constraints=constraints,
        skipped=tuple(collected.skipped),
    )


def dockerfile(base_image: str, index_url: str, *, constraints: bool, source_builds: bool) -> str:
    """The dependencies image: the runner image plus ``uv pip install`` of the two files, which
    are mounted for that step only (no layer of the image holds them)."""
    install = [
        "uv pip install --python /opt/venv/bin/python --no-config --no-cache",
        f"--index-url {index_url} --link-mode copy --requirement /tmp/sensei/requirements.txt",
    ]
    if not source_builds:
        install[0] += " --no-build"
    if constraints:
        install.append("--constraint /tmp/sensei/constraints.txt")
    return "\n".join(
        [
            f"FROM {base_image}",
            "USER root",
            "RUN --mount=type=bind,source=.,target=/tmp/sensei \\\n    "
            + " \\\n    ".join(install),
            "USER 10001:10001",
            "",
        ]
    )


class DependencyImages:
    """Builds (once) and finds the dependencies images of a :class:`Sandbox`.

    An install tries wheels only first. When some package has no wheel and ``source_builds`` is
    on, it's tried again building those from source: their build code runs during the image
    build, which has network (no secrets, nothing of ours mounted; 8.2 puts it behind the
    registry proxy). Off, such a repository gets the limitation instead."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        repository: str = DEPS_IMAGE,
        index_url: str = INDEX_URL,
        source_builds: bool = True,
        timeout: float = INSTALL_TIMEOUT_SECONDS,
    ) -> None:
        if not re.fullmatch(r"https?://[^\s\"'\\]+", index_url):
            raise SandboxError(f"not an index URL: {index_url!r}")
        self._sandbox = sandbox
        self._repository = repository
        self._index_url = index_url
        self._source_builds = source_builds
        self._timeout = timeout
        self._building: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_settings(cls, sandbox: Sandbox, settings: SandboxSettings) -> DependencyImages:
        return cls(
            sandbox,
            index_url=settings.index_url,
            source_builds=settings.source_builds,
            timeout=settings.install_timeout_seconds,
        )

    async def prepare(self, deps: Dependencies) -> Prepared:
        """The image for ``deps``: built when it isn't there yet. Never raises for a repository
        whose dependencies don't install (that's ``limitation``); raises
        :class:`~sensei_engine.sandbox.runner.SandboxUnavailableError` without docker."""
        base = self._sandbox.runner_image(deps.python)
        base_id = await self._sandbox.image_id(base)
        if base_id is None:
            raise SandboxUnavailableError(f"no {base} image: build it with `make sandbox-image`")
        if deps.empty:
            return Prepared(image=base, dependencies=deps, installed=True)
        recipe = dockerfile(base, self._index_url, constraints=True, source_builds=False)
        tag = f"{self._repository}:{deps.key(base_id, recipe)[:32]}"
        async with self._building.setdefault(tag, asyncio.Lock()):
            if await self._sandbox.image_id(tag) is not None:
                return Prepared(image=tag, dependencies=deps, installed=True)
            try:
                async with asyncio.timeout(self._timeout):
                    ok, detail = await self._build(tag, base, deps, source_builds=False)
                    sdists = not ok and _NO_WHEELS in detail
                    if sdists and self._source_builds:
                        ok, detail = await self._build(tag, base, deps, source_builds=True)
            except TimeoutError:
                limitation = f"installing the dependencies took over {self._timeout:g} s"
                return Prepared(
                    image=base, dependencies=deps, installed=False, limitation=limitation
                )
        if not ok:
            limitation = (
                "some dependencies have no wheels (building from source is off)"
                if sdists and not self._source_builds
                else "the dependencies didn't install (see the detail)"
            )
            return Prepared(
                image=base, dependencies=deps, installed=False, limitation=limitation, detail=detail
            )
        return Prepared(
            image=tag, dependencies=deps, installed=True, detail=detail, built=True, sdists=sdists
        )

    async def _build(
        self, tag: str, base: str, deps: Dependencies, *, source_builds: bool
    ) -> tuple[bool, str]:
        """Build ``tag``: whether it built, and what the install said."""
        with tempfile.TemporaryDirectory(prefix="sensei-deps-") as context:
            folder = Path(context)
            (folder / "requirements.txt").write_text("\n".join(deps.requirements) + "\n")
            (folder / "constraints.txt").write_text("\n".join(deps.constraints) + "\n")
            (folder / "Dockerfile").write_text(
                dockerfile(
                    base,
                    self._index_url,
                    constraints=bool(deps.constraints),
                    source_builds=source_builds,
                )
            )
            argv = [
                self._sandbox.docker(),
                "build",
                "--pull=false",
                "--progress=plain",
                f"--label={LABEL}=deps",
                f"--tag={tag}",
                str(folder),
            ]
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=docker_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await process.communicate()
            finally:
                if process.returncode is None:  # cancelled (the time limit)
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()  # the CLI cancels the build on its way out
                    await process.wait()
        return process.returncode == 0, install_output(out)


def install_output(out: bytes) -> str:
    """What the install step printed (docker's step prefixes dropped), its end, redacted."""
    lines = [
        found[1]
        for line in out.decode("utf-8", "replace").splitlines()
        if (found := _STEP_LINE_RE.match(line))
    ]
    text = "\n".join(lines[-MAX_DETAIL_LINES:]) if lines else out.decode("utf-8", "replace")
    return redact_text(text[-LOG_TAIL_BYTES:])
