"""Renamed copies of benchmark repositories, so retrieval can't lean on a model having memorized
the original (public repositories score lower once renamed).

One consistent mapping rewrites the repository's own names (classes, functions, methods,
modules, directories) wherever they appear as whole identifiers: in code, imports, route strings,
comments and paths alike. ``Items`` and ``items`` become ``Zorvat`` and ``zorvat``, and
``ItemsTable`` becomes ``ZorvatTable``... with every word mapped, so names keep their shape and
style (snake_case, camelCase, PascalCase, UPPER_CASE). Replacements never add or remove a line,
so a gold span keeps its line numbers; only its path and symbol change, and
:func:`translate_suite` rewrites them with the same mapping (and the code each query quotes).

The copy is for retrieval evals: it is indexed, not run. Names that also belong to languages,
standard libraries or frameworks are protected, but a repository name that shadows a third-party
API can still be renamed at the call site of that API.

Which names count as the repository's own comes from a symbol source. Until tree-sitter symbol
extraction lands (step 2.4) the default is the labeling parsers in :mod:`sensei_evals.golden`
(exact for Python, top-level declarations and class methods for TypeScript).
"""

from __future__ import annotations

import builtins
import hashlib
import json
import keyword
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from sensei_engine.ingest.ignore import IgnoreRules
from sensei_engine.ingest.walk import walk
from sensei_evals.golden import TS_SUFFIXES, symbols_in
from sensei_evals.schema import GoldTarget, QueryClass, Suite

TOKEN = re.compile(r"(?<![A-Za-z0-9_$])[A-Za-z_$][A-Za-z0-9_$]*")
_WORDS = re.compile(r"[A-Z]+[0-9]*(?=[A-Z][a-z])|[A-Z]?[a-z]+[0-9]*|[A-Z]+[0-9]*|[0-9]+")
_CONSONANTS = "bdfgklmnprstvz"
_VOWELS = "aeiou"
MAX_FILES = 500_000

_JS_NAMES = """
abstract any arguments as async await boolean break case catch class const constructor continue
debugger declare default delete do else enum export extends false finally for from function get
if implements import in infer instanceof interface is keyof let module namespace never new null
number object of package private protected public readonly require return set static string
super switch symbol this throw true try type typeof undefined unique unknown var void while
with yield Array Boolean Date Error JSON Map Math Number Object Promise Proxy Reflect RegExp Set
String Symbol WeakMap WeakSet console document window globalThis process fetch Response Request
URL URLSearchParams Headers FormData localStorage sessionStorage setTimeout clearTimeout
setInterval clearInterval structuredClone length push pop shift unshift slice splice map filter
reduce forEach find findIndex some every includes indexOf join split replace trim toString
toLowerCase toUpperCase keys values entries assign then catch finally resolve reject all
prototype React useState useEffect useMemo useCallback useRef useContext useReducer children
props key ref className style onClick onChange onSubmit value name id title label data error
message status headers body params query path url method options config index main
""".split()  # noqa: SIM905 - a word list reads better as text
PROTECTED = frozenset(
    {
        *keyword.kwlist,
        *keyword.softkwlist,
        *dir(builtins),
        *(
            name
            for t in (str, bytes, dict, list, set, tuple, int, float, object)
            for name in dir(t)
        ),
        *_JS_NAMES,
        "self",
        "cls",
        "__init__",
        "__main__",
        "conftest",
        "setup",
        "README",
        "LICENSE",
        "src",
        "lib",
        "app",
        "apps",
        "tests",
        "test",
        "docs",
        "scripts",
        "public",
        "assets",
        "static",
        "dist",
        "build",
        "bin",
        "node_modules",
        "py",
        "ts",
        "tsx",
        "js",
        "jsx",
        "json",
        "yaml",
        "yml",
        "toml",
        "md",
        "html",
        "css",
        "txt",
        "cfg",
        "ini",
        "lock",
        "env",
    }
)
SymbolSource = Callable[[Path], Iterable[str]]


def split_words(identifier: str) -> list[str]:
    """``getHTTPResponse2`` → ``get, HTTP, Response2``; ``user_id`` → ``user, id``."""
    return [word for part in re.split(r"[_\-$]+", identifier) for word in _WORDS.findall(part)]


def _style(identifier: str) -> Callable[[list[str]], str]:
    core = identifier.strip("_$")
    words = split_words(core)
    if "_" in core and core.isupper():
        return lambda ws: "_".join(w.upper() for w in ws)
    if "_" in core:
        return lambda ws: "_".join(w.lower() for w in ws)
    if "-" in core:
        return lambda ws: "-".join(w.lower() for w in ws)
    if core.isupper() and len(words) == 1:
        return lambda ws: "".join(w.upper() for w in ws)
    if core[:1].isupper():
        return lambda ws: "".join(w.capitalize() for w in ws)
    return lambda ws: ws[0].lower() + "".join(w.capitalize() for w in ws[1:])


def _pseudo_word(word: str, seed: int, attempt: int) -> str:
    digest = hashlib.sha256(f"{seed}:{word.lower()}:{attempt}".encode()).digest()
    syllables = max(2, min(4, (len(word) + 2) // 3))
    return "".join(
        _CONSONANTS[digest[2 * i] % len(_CONSONANTS)] + _VOWELS[digest[2 * i + 1] % len(_VOWELS)]
        for i in range(syllables)
    )


@dataclass(frozen=True, slots=True)
class RenamePlan:
    seed: int
    tokens: dict[str, str]  # whole identifier → new identifier
    words: dict[str, str] = field(default_factory=dict[str, str])  # lowercase word → pseudo word

    def sub(self, text: str) -> str:
        """``text`` with every mapped whole identifier replaced."""
        return TOKEN.sub(lambda m: self.tokens.get(m[0], m[0]), text)

    def path(self, path: str) -> str:
        return "/".join(self.sub(part) for part in PurePosixPath(path).parts)


def make_plan(names: Iterable[str], existing: Iterable[str], *, seed: int = 0) -> RenamePlan:
    """A consistent mapping for ``names``; new names never collide with ``existing`` tokens (all
    identifiers in the repository) or with each other."""
    taken = set(existing)
    taken_lower = {token.lower() for token in taken}
    words: dict[str, str] = {}
    tokens: dict[str, str] = {}

    def word_for(word: str) -> str:
        key = word.lower()
        if key not in words:
            if key.isdigit():
                words[key] = key
            else:
                attempt = 0
                while (pseudo := _pseudo_word(key, seed, attempt)) in taken_lower or (
                    pseudo in words.values()
                ):
                    attempt += 1
                words[key] = pseudo
        return words[key]

    for name in sorted(set(names)):
        if name in PROTECTED or name.startswith("__") or not TOKEN.fullmatch(name):
            continue
        parts = split_words(name)
        if not parts:
            continue
        prefix = name[: len(name) - len(name.lstrip("_$"))]
        suffix = name[len(name.rstrip("_$")) :]
        renamed = prefix + _style(name)([word_for(w) for w in parts]) + suffix
        n = 2
        candidate = renamed
        while candidate in taken or candidate in tokens.values():
            candidate = f"{renamed}{n}"
            n += 1
        tokens[name] = candidate
    return RenamePlan(seed=seed, tokens=tokens, words=words)


# ---------------------------------------------------------------- sources of names


def labeling_symbols(checkout: Path) -> list[str]:
    """Names the repository defines, from the labeling parsers (until tree-sitter symbols):
    definitions (each part of ``Class.method``), module file stems and directory names."""
    files, _ = walk(checkout, IgnoreRules(()), max_files=MAX_FILES)
    names: set[str] = set()
    for file in files:
        path = PurePosixPath(file.path)
        names.update(part for part in path.parent.parts if not part.startswith("."))
        if path.suffix in {".py", *TS_SUFFIXES}:
            names.add(path.stem.split(".")[0])
            for span in symbols_in(checkout, file.path):
                names.update(span.name.split("."))
    return sorted(names)


def symbols_from_file(path: Path) -> list[str]:
    """One name per line; blank lines and ``#`` comments are skipped."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


# ---------------------------------------------------------------- the copy


@dataclass(slots=True)
class RenameResult:
    plan: RenamePlan
    paths: dict[str, str] = field(default_factory=dict[str, str])  # old → new, every file
    rewritten: int = 0  # text files whose content changed
    copied: int = 0  # files copied byte for byte (binary or unchanged)
    sha: str | None = None  # the copy's commit, once committed
    source: dict[str, str] = field(default_factory=dict[str, str])  # repo and sha it was made from

    def mapping(self) -> dict[str, object]:
        return {
            "source": self.source,
            "seed": self.plan.seed,
            "sha": self.sha,
            "tokens": self.plan.tokens,
            "words": self.plan.words,
            "paths": self.paths,
        }


def repository_tokens(checkout: Path) -> set[str]:
    files, _ = walk(checkout, IgnoreRules(()), max_files=MAX_FILES)
    tokens: set[str] = set()
    for file in files:
        tokens.update(TOKEN.findall(file.path))
        try:
            tokens.update(TOKEN.findall(file.absolute.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return tokens


def rename_tree(source: Path, target: Path, plan: RenamePlan) -> RenameResult:
    """Write the renamed copy of ``source`` (its files the indexer would keep, without ``.git``
    and symbolic links) to the new, empty directory ``target``."""
    if target.exists() and any(target.iterdir()):
        msg = f"{target} exists and is not empty"
        raise FileExistsError(msg)
    files, _ = walk(source, IgnoreRules(()), max_files=MAX_FILES)
    result = RenameResult(plan=plan)
    for file in files:
        new_path = plan.path(file.path)
        if new_path in result.paths.values():
            msg = f"two files would both become {new_path}"
            raise ValueError(msg)
        result.paths[file.path] = new_path
        destination = target / new_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = file.absolute.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            destination.write_bytes(raw)
            result.copied += 1
            continue
        renamed = plan.sub(text)
        if renamed.count("\n") != text.count("\n"):  # pragma: no cover - TOKEN can't match "\n"
            msg = f"renaming {file.path} changed its line count"
            raise AssertionError(msg)
        destination.write_bytes(renamed.encode("utf-8"))
        shutil.copymode(file.absolute, destination)
        if renamed != text:
            result.rewritten += 1
        else:
            result.copied += 1
    return result


def commit_tree(directory: Path) -> str:
    """Commit the copy as a fresh repository with a fixed author and date, so the same copy
    always gets the same sha. Returns it."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "sensei-eval",
        "GIT_AUTHOR_EMAIL": "sensei-eval@localhost",
        "GIT_COMMITTER_NAME": "sensei-eval",
        "GIT_COMMITTER_EMAIL": "sensei-eval@localhost",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    git = [
        "git",
        "-C",
        str(directory),
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "renamed copy"]):
        subprocess.run([*git, *args], check=True, env=env, timeout=600)  # noqa: S603
    head = subprocess.run(  # noqa: S603
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
    )
    return head.stdout.strip()


def write_mapping(result: RenameResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.mapping(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- gold


def translate_target(target: GoldTarget, result: RenameResult) -> GoldTarget:
    path = result.paths.get(target.path)
    if path is None:
        msg = f"gold path {target.path} is not in the renamed copy"
        raise KeyError(msg)
    symbol = result.plan.sub(target.symbol) if target.symbol is not None else None
    return target.model_copy(update={"path": path, "symbol": symbol})


_BACKTICKED = re.compile(r"`([^`]+)`")


def translate_query(query: str, query_class: QueryClass, plan: RenamePlan) -> str:
    """Symbol and regex queries quote the code, so all of them is renamed; the others are plain
    language written without identifiers, so only what they quote in backticks is."""
    if query_class in {QueryClass.SYMBOL, QueryClass.REGEX}:
        return plan.sub(query)
    return _BACKTICKED.sub(lambda m: f"`{plan.sub(m[1])}`", query)


def translate_suite(suite: Suite, repo: str, result: RenameResult, *, new_repo: str) -> Suite:
    """The suite's items for ``repo``, pointed at the renamed copy: paths, symbols and queries
    rewritten with the copy's mapping, lines unchanged, the sha the copy's commit."""
    if result.sha is None:
        msg = "commit the renamed copy before translating gold to it"
        raise ValueError(msg)
    items = [
        item.model_copy(
            update={
                "id": f"{item.id}.renamed",
                "repo": new_repo,
                "sha": result.sha,
                "query": translate_query(item.query, item.class_, result.plan),
                "gold": [translate_target(t, result) for t in item.gold],
            }
        )
        for item in suite.for_repo(repo)
    ]
    return Suite(name=f"{suite.name}-renamed", version=suite.version, items=items)


def suite_yaml(suite: Suite) -> str:
    """A suite as YAML in the golden file layout."""
    items: list[dict[str, object]] = []
    for item in suite.items:
        data: dict[str, object] = {
            "id": item.id,
            "repo": item.repo,
            "sha": item.sha,
            "query": item.query,
            "class": item.class_.value,
        }
        if item.layers:
            data["layers"] = [layer.value for layer in item.layers]
        if item.gold:
            data["gold"] = [t.model_dump(mode="json", exclude_none=True) for t in item.gold]
        if not item.answerable:
            data["answerable"] = False
        if item.notes:
            data["notes"] = item.notes
        items.append(data)
    document = {"name": suite.name, "version": suite.version, "items": items}
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
