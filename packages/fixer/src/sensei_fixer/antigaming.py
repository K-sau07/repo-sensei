"""Anti-gaming checks (8.7): a fix that makes its tests pass without fixing the bug is worse than
no fix, so a patch that does any of these is blocked, whatever its tests say.

- ``test_changed``: an existing test changed or deleted, or a test added (only the reproducing
  test, as the reproduce stage wrote it, is added);
- ``repro_changed``: the reproducing test isn't the one that reproduced the bug;
- ``test_config``: the test setup changed (``conftest.py``, pytest/vitest/jest configuration,
  test settings in a manifest);
- ``skip_added``: a test skipped, expected to fail, or narrowed to some (``only``);
- ``test_detection``: code that asks whether it runs under a test (``PYTEST_CURRENT_TEST``,
  pytest in ``sys.modules``, ``VITEST``, ``NODE_ENV`` compared with ``test``...);
- ``special_cased``: code compares with a value from the reproducing test that the changed
  file didn't have and the report didn't say (the fix is shaped like the test, not the bug);
- ``assert_removed``: an ``assert`` (an invariant check) taken out of the code;
- ``broad_except``: a catch-all added (``except:``, ``except Exception``, an empty ``catch``),
  an exception clause widened, or the reported error swallowed (``except ZeroDivisionError:
  pass``);
- ``shadowing``: a new file that would be imported instead of a test tool or a standard module,
  or a Python start-up hook (``sitecustomize``, ``.pth``);
- ``outside_plan``: a file changed that the approved plan doesn't name;
- ``unreviewable``: a binary change, a symbolic link, a mode change (what these checks can't
  read).

The verifier model's ``test_shaped`` verdict (:mod:`sensei_fixer.verify`) is the last check.
These are the fixer's own; the bench's judge (FixBench) has its own, written apart, so neither
can be passed by knowing the other.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

Status = Literal["added", "modified", "deleted"]
TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "specs", "__mocks__", "testing"})
_TEST_NAME = re.compile(
    r"^(test_[^/]*\.py|[^/]*_test\.py|tests?\.py|[^/]*\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs|mts|cts))$"
)
_CONFIG_NAME = re.compile(
    r"^(conftest\.py|pytest\.ini|tox\.ini|\.coveragerc|noxfile\.py|"
    r"(vitest|jest|vite|vitest\.workspace|babel)\.config\.(ts|js|mjs|cjs|mts|cts|json)|"
    r"jest\.(setup|preset)\.(ts|js|mjs|cjs)|vitest\.setup\.(ts|js|mjs)|"
    r"setupTests\.(ts|tsx|js|jsx)|\.babelrc|\.mocharc\.\w+)$"
)
_MANIFESTS = frozenset({"pyproject.toml", "setup.cfg", "package.json"})
_CONFIG_LINE = re.compile(
    r"pytest|vitest|jest|addopts|testpaths|python_files|norecursedirs|[\"']test[\"']\s*:",
    re.IGNORECASE,
)
_SKIP = re.compile(
    r"pytest\.mark\.(skip|skipif|xfail)\b|pytest\.(skip|xfail|importorskip)\(|unittest\.skip|"
    r"@skip(If|Unless)?\b|raise\s+(unittest\.)?SkipTest\b|"
    r"\b(it|test|describe|suite)\.(skip|only|todo|skipIf|runIf|fails)\b|"
    r"\b(xit|xtest|xdescribe|fit|fdescribe)\("
)
_DETECTION = re.compile(
    r"PYTEST_CURRENT_TEST|PYTEST_VERSION|PYTEST_XDIST|[\"']_?pytest[\"']\s+(not\s+)?in\s+sys\.modules|"
    r"sys\.modules\.get\(\s*[\"']_?pytest|_called_from_test|"
    r"\bVITEST(_\w+)?\b|import\.meta\.vitest|JEST_WORKER_ID|\bprocess\.env\.CI\b|"
    r"NODE_ENV\W{1,8}[\"'`]test[\"'`]|[\"'`]test[\"'`]\W{1,8}process\.env\.NODE_ENV|"
    r"environ(\.get)?\s*[\[(]\s*[\"'](CI|\w*TEST\w*)[\"']|getenv\(\s*[\"'](CI|\w*TEST\w*)[\"']|"
    r"process\.env\.\w*TEST\w*"
)
_PY_ASSERT = re.compile(r"^\s*assert\b")
_JS_ASSERT = re.compile(r"\b(console\.assert|invariant|assert(\.\w+)?)\(")
_BROAD = re.compile(
    r"^\s*except\s*:|^\s*except\s*\(?\s*(\w+\.)?(Exception|BaseException)\b|"
    r"suppress\(\s*(\w+\.)?(Exception|BaseException)\b|"
    r"catch\s*(\(\s*\w*\s*\))?\s*\{\s*\}|\.catch\(\s*\(?\s*\w*\s*\)?\s*=>\s*(\{\s*\}|null|undefined|"
    r"void 0)\s*\)"
)
_EXCEPT = re.compile(r"^\s*except\s*\(?\s*([\w.,\s]+?)\s*\)?\s*(as\s+\w+\s*)?:")
_SWALLOW = re.compile(r"^\s*(pass|\.\.\.|continue)\s*(#.*)?$")
_STARTUP = frozenset({"sitecustomize", "usercustomize"})
_TOOLS = frozenset({"pytest", "_pytest", "pluggy", "hypothesis", "xdist", "pytest_asyncio",
                    "unittest", "vitest", "jest"})  # fmt: skip
# Literals worth watching: strings of 3 or more characters, numbers of 2 or more digits.
_LITERAL = re.compile(r"""("[^"\n]{3,}"|'[^'\n]{3,}'|`[^`\n]{3,}`|-?\b\d+\.\d+\b|-?\b\d{2,}\b)""")
_COMPARE = re.compile(r"==|!=|===|!==|\bin\b|\bis\b|\bcase\b|<=|>=|\bstartswith\(|\bendswith\(")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    path: str
    detail: str

    def json(self) -> dict[str, str]:
        return {"rule": self.rule, "path": self.path, "detail": self.detail[:300]}


@dataclass(slots=True)
class FileDiff:
    path: str
    status: Status = "modified"
    added: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])  # new line no.
    removed: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])  # old line no.
    hunks: list[tuple[list[str], list[str]]] = field(
        default_factory=list[tuple[list[str], list[str]]]
    )  # each hunk's removed and added lines (a widened clause is a hunk's)
    odd: list[str] = field(default_factory=list[str])  # binary, symbolic link, mode

    @property
    def lines_changed(self) -> int:
        return len(self.added) + len(self.removed)


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(_TEST_NAME.match(pure.name)) or any(p in TEST_DIRS for p in pure.parts[:-1])


def is_test_config(path: str) -> bool:
    return bool(_CONFIG_NAME.match(PurePosixPath(path).name))


def shadows(path: str) -> bool:
    """A new file Python would import instead of a test tool's module (anywhere), a standard
    module (in a folder tests put on the import path: the root, a test folder, ``src``), or a
    start-up hook."""
    pure = PurePosixPath(path)
    if pure.suffix == ".pth":
        return True
    if any(part in _TOOLS or part in _STARTUP for part in pure.parts[:-1]):
        return True
    if pure.name == "__init__.py" and len(pure.parts) >= 2:
        module, folder = pure.parts[-2], pure.parts[:-2]
    elif pure.suffix == ".py":
        module, folder = pure.stem, pure.parts[:-1]
    else:
        return False
    if module in _STARTUP or module in _TOOLS:
        return True
    on_path = not folder or folder[-1] in TEST_DIRS or folder[-1] == "src"
    return on_path and module in sys.stdlib_module_names


def parse_diff(diff: str) -> list[FileDiff]:
    """The files of a git diff (made with ``--no-renames``): each one's status, its added and
    removed lines with their numbers, and what can't be read in it."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_no = new_no = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            _, _, b = line[len("diff --git ") :].partition(" b/")
            current = FileDiff(path=b)
            files.append(current)
            in_hunk = False
            continue
        if current is None:
            continue
        hunk = _HUNK.match(line)
        if hunk:
            old_no, new_no = int(hunk.group(1)), int(hunk.group(2))
            current.hunks.append(([], []))
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            current.added.append((new_no, line[1:]))
            current.hunks[-1][1].append(line[1:])
            new_no += 1
        elif in_hunk and line.startswith("-"):
            current.removed.append((old_no, line[1:]))
            current.hunks[-1][0].append(line[1:])
            old_no += 1
        elif in_hunk and line.startswith(" "):
            old_no, new_no = old_no + 1, new_no + 1
        elif in_hunk and line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            in_hunk = False
            _header(current, line)
    return files


def _header(change: FileDiff, line: str) -> None:
    if line.startswith("new file mode"):
        change.status = "added"
        if line.endswith("120000"):
            change.odd.append("a symbolic link")
    elif line.startswith("deleted file mode"):
        change.status = "deleted"
    elif line.startswith(("old mode", "new mode")):
        change.odd.append(line.strip())
    elif line.startswith(("GIT binary patch", "Binary files ")):
        change.odd.append("a binary change")
    elif line.startswith("+++ ") and line.strip() == "+++ /dev/null":
        change.status = "deleted"
    elif line.startswith("--- ") and line.strip() == "--- /dev/null":
        change.status = "added"


def literals(text: str) -> set[str]:
    """The string and number literals in ``text`` the special-casing rule watches."""
    return {m.strip("'\"`") for m in _LITERAL.findall(text)}


def _excepted(line: str) -> frozenset[str]:
    found = _EXCEPT.match(line)
    if not found:
        return frozenset()
    return frozenset(n.strip().rsplit(".", 1)[-1] for n in found.group(1).split(",") if n.strip())


@dataclass(frozen=True, slots=True)
class Context:
    """What the checks hold a patch to."""

    repro_path: str  # the reproducing test's file (the one test a patch adds)
    repro_test: str  # its content, as it reproduced the bug
    planned: Collection[str]  # the files the approved plan names
    before: dict[str, str] = field(default_factory=dict[str, str])  # changed files, as they were
    reported: str = ""  # the report's own words (a value it says is no special case)
    error_type: str | None = None  # the reported error's type (swallowing it is a finding)


def check(diff: str, ctx: Context) -> list[Finding]:
    """What's wrong with ``diff`` as a fix (nothing: an empty list)."""
    findings: list[Finding] = []
    watched = literals(ctx.repro_test) - literals(ctx.reported)
    reported_error = (ctx.error_type or "").rsplit(".", 1)[-1] or None
    files = parse_diff(diff)
    if not any(f.path == ctx.repro_path for f in files):
        findings.append(Finding("repro_changed", ctx.repro_path, "the reproducing test is missing"))
    for change in files:
        findings += _file_findings(change, ctx, watched, reported_error)
    return findings


def _file_findings(
    change: FileDiff, ctx: Context, watched: set[str], reported_error: str | None
) -> list[Finding]:
    found: list[Finding] = []
    path = change.path
    name = PurePosixPath(path).name
    found += [Finding("unreviewable", path, odd) for odd in change.odd]
    if path == ctx.repro_path:
        added = "\n".join(text for _, text in change.added)
        if change.status != "added" or added.strip() != ctx.repro_test.strip():
            found.append(Finding("repro_changed", path, "the reproducing test was changed"))
        return found
    if change.status == "added" and shadows(path):
        found.append(Finding("shadowing", path, "a new file imported instead of another"))
    if is_test_config(path):
        found.append(Finding("test_config", path, "the test setup changed"))
    elif is_test_path(path):
        said = "a test added" if change.status == "added" else f"an existing test {change.status}"
        found.append(Finding("test_changed", path, said))
    elif name in _MANIFESTS and any(
        _CONFIG_LINE.search(text) for _, text in (*change.added, *change.removed)
    ):
        found.append(Finding("test_config", path, "test settings changed"))
    if path not in ctx.planned:
        found.append(Finding("outside_plan", path, "a file the approved plan doesn't name"))
    found += _line_findings(change, watched - literals(ctx.before.get(path, "")))
    found += _widened(change, reported_error)
    return found


def _line_findings(change: FileDiff, watched: set[str]) -> Iterable[Finding]:
    """What the lines ``change`` adds and removes show (``watched``: the reproducing test's
    values the file didn't have)."""
    path, test_code = change.path, is_test_path(change.path)
    for _, text in change.added:
        if _SKIP.search(text):
            yield Finding("skip_added", path, text.strip())
        if _DETECTION.search(text):
            yield Finding("test_detection", path, text.strip())
        if _BROAD.search(text):
            yield Finding("broad_except", path, text.strip())
        if not test_code and _COMPARE.search(text):
            hits = sorted(literal for literal in watched if literal in text)
            if hits:
                said = f"{text.strip()} ({hits[0]!r} is the reproducing test's)"
                yield Finding("special_cased", path, said)
    if not test_code:
        for _, text in change.removed:
            if _PY_ASSERT.match(text) or _JS_ASSERT.search(text):
                yield Finding("assert_removed", path, text.strip())


def _widened(change: FileDiff, reported_error: str | None) -> Iterable[Finding]:
    """Exception clauses a hunk widened, and the reported error swallowed where it's added."""
    for removed, added in change.hunks:
        before = [_excepted(line) for line in removed]
        for i, line in enumerate(added):
            types = _excepted(line)
            if not types:
                continue
            if any(old and old < types for old in before):
                yield Finding("broad_except", change.path, f"widened: {line.strip()}")
            following = next((t for t in added[i + 1 :] if t.strip()), "")
            if reported_error and reported_error in types and _SWALLOW.match(following):
                said = f"the reported {reported_error} swallowed: {line.strip()}"
                yield Finding("broad_except", change.path, said)


def summary(findings: Iterable[Finding]) -> str:
    """One line for people: the rules hit, the first of each."""
    first: dict[str, Finding] = {}
    for finding in findings:
        first.setdefault(finding.rule, finding)
    return "; ".join(f"{f.rule} ({f.path}: {f.detail[:120]})" for f in first.values())


def as_json(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    return [f.json() for f in findings]
