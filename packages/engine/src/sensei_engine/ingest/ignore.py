"""Which paths indexing skips, and the ``linguist-*`` attributes a repository sets on its files.

Ignore rules come from four sources, strongest first: the repository's settings (ignore globs an
admin entered, relative to the root), ``.senseiignore`` files, ``.gitignore`` files and a built-in
deny list (dependencies, build output, lockfiles). Within a source the deepest file with a matching
rule decides and its last matching rule wins, as in git; a stronger source overrides a weaker one,
so ``!vendor/`` in a ``.senseiignore`` brings back a directory the built-ins skip. An ignored
directory isn't entered, so nothing under it can be re-included (git behaves the same way).

``.gitattributes`` files may mark paths ``linguist-generated``, ``linguist-vendored`` or
``linguist-documentation`` (GitHub's convention); deeper files and later lines win.

Patterns are compiled by pathspec but matched the way git matches them while walking: a pattern
counts for a path only if it matches that path itself. pathspec also lets a directory pattern
(``build/``, ``!build/``) speak for everything inside the directory, which is right for matching
a flat file list but wrong here: the walk already decided the directory, and ``!build/`` must not
re-include ``build/node_modules``. ``a/**`` matches what is inside ``a``, not ``a`` itself.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pathspec import GitIgnoreSpec
from pathspec.pattern import RegexPattern

BUILTIN_DENY: tuple[str, ...] = (
    # dependencies and virtual environments
    "node_modules/",
    "bower_components/",
    "vendor/",
    ".venv/",
    "venv/",
    # build output and caches
    "dist/",
    "build/",
    "out/",
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    ".turbo/",
    ".parcel-cache/",
    "coverage/",
    "__pycache__/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    ".gradle/",
    "*.pyc",
    "*.pyo",
    # version control
    ".git",
    # minified bundles, source maps and lockfiles
    "*.min.js",
    "*.map",
    "*.lock",
    "*.lockb",
    "package-lock.json",
    "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
)

IGNORE_FILES = (".senseiignore", ".gitignore")
ATTRIBUTES_FILE = ".gitattributes"
# Ignore and attribute files bigger than this are read only this far.
MAX_RULES_BYTES = 256 * 1024


class Attribute(enum.StrEnum):
    GENERATED = "linguist-generated"
    VENDORED = "linguist-vendored"
    DOCUMENTATION = "linguist-documentation"


def _compile(lines: Iterable[str]) -> tuple[GitIgnoreSpec, list[str]]:
    """The spec of the valid lines, and the lines that aren't valid patterns (skipped, as git
    skips them)."""
    valid: list[str] = []
    invalid: list[str] = []
    for line in lines:
        try:
            GitIgnoreSpec.from_lines([line])
        except ValueError:
            invalid.append(line)
        else:
            valid.append(line)
    return GitIgnoreSpec.from_lines(valid), invalid


def read_rules(path: Path) -> list[str]:
    """The lines of an ignore or attributes file (a symlink or a non-file reads as empty)."""
    if path.is_symlink() or not path.is_file():
        return []
    with path.open("rb") as handle:
        data = handle.read(MAX_RULES_BYTES)
    return data.decode("utf-8-sig", "replace").splitlines()


def _patterns(spec: GitIgnoreSpec) -> list[RegexPattern]:
    return cast("list[RegexPattern]", spec.patterns)


_DIR_MARK = "ps_d"  # pathspec's group for "the slash after a matched directory"


def matches(pattern: RegexPattern, path: str, *, is_dir: bool) -> bool:
    """Whether ``pattern`` matches ``path`` itself (not as something inside a matched
    directory). ``path`` is relative to the pattern's file, without a trailing slash."""
    # pathspec's own annotations leave these partly unknown.
    regex = cast("re.Pattern[str] | None", getattr(pattern, "regex", None))
    source = str(getattr(pattern, "pattern", ""))
    if regex is None:
        return False
    marked = _DIR_MARK in regex.groupindex
    candidate = path
    if is_dir and (marked or not source.rstrip().endswith("/**")):
        candidate = f"{path}/"  # "a/**" is for what's inside a: test "a" as is
    position = 0
    while (match := regex.search(candidate, position)) is not None:
        if not marked or match.group(_DIR_MARK) is None or match.end(_DIR_MARK) == len(candidate):
            return True
        position = match.start() + 1  # a later occurrence may match the path itself
    return False


@dataclass(frozen=True, slots=True)
class _Rules:
    base: str  # the directory the patterns are relative to ("" for the root)
    spec: GitIgnoreSpec

    def decide(self, path: str, *, is_dir: bool) -> bool | None:
        """True: ignored; False: re-included; None: no pattern here says (the last match wins)."""
        relative = path[len(self.base) + 1 :] if self.base else path
        for pattern in reversed(_patterns(self.spec)):
            if pattern.include is not None and matches(pattern, relative, is_dir=is_dir):
                return pattern.include
        return None


@dataclass(frozen=True, slots=True)
class _AttributeRule:
    base: str
    spec: GitIgnoreSpec  # the line's single pattern
    values: tuple[tuple[Attribute, bool], ...]


def _attribute_rules(base: str, lines: Iterable[str]) -> list[_AttributeRule]:
    rules: list[_AttributeRule] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue  # negative patterns aren't allowed in .gitattributes
        pattern, *assignments = line.split()
        values: list[tuple[Attribute, bool]] = []
        for assignment in assignments:
            name, _, value = assignment.partition("=")
            unset = name.startswith(("-", "!"))
            name = name.lstrip("-!")
            if name in Attribute._value2member_map_:
                enabled = not unset and value.lower() not in ("false", "0", "no")
                values.append((Attribute(name), enabled))
        if not values or pattern.endswith("/"):
            continue  # git applies no attributes through a directory pattern
        spec, invalid = _compile([pattern])
        if not invalid:
            rules.append(_AttributeRule(base, spec, tuple(values)))
    return rules


@dataclass(frozen=True, slots=True)
class Scope:
    """The rules in force inside one directory (its own files plus its ancestors')."""

    sensei: tuple[_Rules, ...] = ()
    git: tuple[_Rules, ...] = ()
    attributes: tuple[_AttributeRule, ...] = ()


class IgnoreRules:
    def __init__(self, settings_globs: Sequence[str] = ()) -> None:
        self._builtin = _Rules("", GitIgnoreSpec.from_lines(BUILTIN_DENY))
        spec, self.invalid_settings_globs = _compile(settings_globs)
        self._settings = _Rules("", spec)

    def enter(self, scope: Scope, directory: Path, relative: str) -> Scope:
        """The scope inside ``directory`` (at ``relative``, "" for the root), adding the ignore and
        attribute files it contains."""
        sensei, git = scope.sensei, scope.git
        for name in IGNORE_FILES:
            lines = read_rules(directory / name)
            if lines:
                rules = _Rules(relative, _compile(lines)[0])
                if name == ".senseiignore":
                    sensei = (*sensei, rules)
                else:
                    git = (*git, rules)
        attributes = scope.attributes
        lines = read_rules(directory / ATTRIBUTES_FILE)
        if lines:
            attributes = (*attributes, *_attribute_rules(relative, lines))
        return Scope(sensei, git, attributes)

    def ignored(self, scope: Scope, path: str, *, is_dir: bool) -> bool:
        """Whether ``path`` (relative, POSIX, no trailing slash) is skipped."""
        decision = self._settings.decide(path, is_dir=is_dir)
        if decision is None:
            decision = _deepest(scope.sensei, path, is_dir=is_dir)
        if decision is None:
            decision = _deepest(scope.git, path, is_dir=is_dir)
        if decision is None:
            decision = self._builtin.decide(path, is_dir=is_dir)
        return bool(decision)

    @staticmethod
    def attributes(scope: Scope, path: str) -> frozenset[Attribute]:
        """The ``linguist-*`` attributes set on the file at ``path``."""
        state: dict[Attribute, bool] = {}
        for rule in scope.attributes:  # shallow to deep, in file order: later rules win
            relative = path[len(rule.base) + 1 :] if rule.base else path
            if any(matches(p, relative, is_dir=False) for p in _patterns(rule.spec)):
                state.update(rule.values)
        return frozenset(attribute for attribute, enabled in state.items() if enabled)


def _deepest(layers: Sequence[_Rules], path: str, *, is_dir: bool) -> bool | None:
    for rules in reversed(layers):
        decision = rules.decide(path, is_dir=is_dir)
        if decision is not None:
            return decision
    return None
