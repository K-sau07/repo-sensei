"""Renamed copies of benchmark repositories, so retrieval can't lean on a model having memorized
the original (public repositories score lower once renamed).

One consistent mapping rewrites the repository's own names (classes, functions, methods, class
attributes, modules, directories) wherever they appear as whole identifiers: in code, route
strings and comments. Every word gets one pseudo word and names keep their shape and style
(snake_case, camelCase, PascalCase, UPPER_CASE): with ``Items`` → ``Zorvat``, ``ItemsTable``
becomes ``ZorvatTable`` and ``items_table`` ``zorvat_table``.

What is not renamed, and why, is written to the mapping file (``kept``):

- names that also belong to a language, standard library or framework (``items`` is also
  ``dict.items``, ``index`` and ``private`` are JavaScript words): renaming them would rename
  every use of those too;
- names imported from a library (sqlmodel's ``Field``, lucide-react's ``Settings``), in the
  language that imports them. Each name is also renamed only in code of the languages that
  define it (and in non-code files), so a Python class ``Settings`` leaves a TypeScript icon of
  that name alone.

Paths are not code: every directory and file name is renamed word by word through the word map,
kebab-case and protected words included (``items/list.py`` → ``zorvat/kinoru.py``,
``recover-password.tsx`` → ``dabe-kinoru.tsx``); extensions, conventional names (``index``,
``__init__``, ``src``, dotfiles) stay. The words the indexer reads a file's role from
(``routes``, ``tests``, ``test_``, ``models``, ``alembic``: :data:`ROLE_WORDS`) are never
replaced in paths, nor in code names that are also a module, file or directory name (``crud``,
``user_settings`` → ``bemi_settings``): an import names its module as its file does. Every other
code name is renamed whole, role words included (``Settings`` → ``Fadu``, ``api_router`` →
``vope_kuta``), except a hook's leading ``use`` (``useAuth`` → ``useVipe``), which gives a
definition its kind. So each file keeps its role and each definition its kind.
Dependency manifests are copied unchanged (:func:`is_verbatim`), so the same frameworks are
detected.

Import statements are rewritten the same way, so every import still resolves in the copy:
Python ``import``/``from ... import`` of the repository's own modules (binding the old name with
``as`` where the module's code name is kept), and relative or ``@/`` specifiers in TypeScript
and JavaScript. Library imports are left alone.

Replacements never add or remove a line, so a gold span keeps its line numbers; only its path
and symbol change, and :func:`translate_suite` rewrites them with the same mapping (and the code
each query quotes). The copy is for retrieval evals: it is indexed, not run, and a name shared
with a library's attribute or keyword argument can still be renamed where that library is used.

Which names count as the repository's own comes from a symbol source. The default is the
indexer's tree-sitter symbol table (:func:`extracted_symbols`), so every definition the index
knows is renamed; the labeling parsers in :mod:`sensei_evals.golden` (exact for Python,
top-level declarations and class methods for TypeScript) are the other choice.
"""

from __future__ import annotations

import ast
import enum
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml

from sensei_engine.ingest.frameworks import is_manifest
from sensei_engine.ingest.ignore import IgnoreRules
from sensei_engine.ingest.languages import detect_language
from sensei_engine.ingest.walk import walk
from sensei_engine.parse.parser import parse_file
from sensei_engine.parse.stage import parseable, read_source
from sensei_engine.symbols.extract import SymbolKind, extract_symbols
from sensei_evals.golden import MAX_FILES, TS_SUFFIXES, Problem, parse_python, symbols_in
from sensei_evals.python_names import PYTHON_NAMES
from sensei_evals.repos import mapping_path, read_mapping
from sensei_evals.schema import (
    GoldenItem,
    GoldTarget,
    QueryClass,
    RenamedFrom,
    Suite,
    SuiteError,
    parse_suite,
)

TOKEN = re.compile(r"(?<![A-Za-z0-9_$])[A-Za-z_$][A-Za-z0-9_$]*")
# In a regex query an escape is consumed whole, and one that matches a boundary or a character
# class (\b, \w, \s, \d and their negations) ends right before a name: ``\bLoginService\b``.
REGEX_TOKEN = re.compile(r"\\.|(?:(?<![A-Za-z0-9_$])|(?<=\\[bBwWsSdD]))[A-Za-z_$][A-Za-z0-9_$]*")
_WORDS = re.compile(r"[A-Z]+[0-9]*(?=[A-Z][a-z])|[A-Z]?[a-z]+[0-9]*|[A-Z]+[0-9]*|[0-9]+")
_CONSONANTS = "bdfgklmnprstvz"
_VOWELS = "aeiou"

PYTHON = "python"
JS = "js"  # JavaScript and TypeScript
CODE_LANGUAGES = frozenset({PYTHON, JS})
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})

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
# Directory and file names that tools and readers look for, or that every repository shares.
_CONVENTIONAL = """
__init__ __main__ conftest setup index main README LICENSE CHANGELOG CONTRIBUTING SECURITY
Dockerfile Makefile Procfile package package-lock tsconfig jsconfig pyproject requirements
src lib app apps packages tests test docs scripts public assets static dist build bin
node_modules
""".split()  # noqa: SIM905
PROTECTED = frozenset(
    {
        *PYTHON_NAMES,
        *_JS_NAMES,
        *_CONVENTIONAL,
        "self",
        "cls",
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
PATH_KEPT = frozenset(_CONVENTIONAL)
# Words the indexer reads a file's role from (ingest/roles.py: test, docs, config, db and api
# directories and file names, and the names of docs like CHANGES or SECURITY), so every file of
# the copy has its original's role. Generic, they don't give a repository away.
ROLE_WORDS = frozenset(
    """
    test tests spec specs e2e cypress playwright mocks docs doc documentation scripts settings
    migrations migration alembic models model repositories repository db database crud api
    routes route routers router controllers controller endpoints handlers views urls resolver
    config conf setup manage noxfile tasks fabfile gulpfile gruntfile entity schema pages server
    readme changelog changes history contributing license licence copying notice authors code
    conduct security support release notes releasing
    """.split()  # noqa: SIM905
)
# Words never replaced in paths, nor in code names that are also module, file or directory names
# (an import and its file must keep the same name): the role words, and ``use`` (hook files).
KEPT_WORDS = ROLE_WORDS | {"use"}
# A React hook's name: its leading ``use`` stays (React and the symbol extractor read the kind).
_HOOK = re.compile(r"use(?=[A-Z0-9])")
KEPT_PROTECTED = (
    "kept in code: also a language, standard library or framework name, so renaming it would "
    "rename those uses too (file and directory names are still renamed)"
)

Names = Mapping[str, Iterable[str]] | Iterable[str]  # name → languages defining it, or names
SymbolSource = Callable[[Path], Names]


def language_of(path: str) -> str | None:
    """``python``, ``js`` (JavaScript and TypeScript) or None for a non-code file."""
    suffix = PurePosixPath(path).suffix
    if suffix in PYTHON_SUFFIXES:
        return PYTHON
    return JS if suffix in TS_SUFFIXES else None


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
    if core.isupper() and len(words) == 1:
        return lambda ws: "".join(w.upper() for w in ws)
    if core[:1].isupper():
        return lambda ws: "".join(w.capitalize() for w in ws)
    return lambda ws: ws[0].lower() + "".join(w.capitalize() for w in ws[1:])


def _restyle(identifier: str, word: Callable[[str], str | None]) -> str | None:
    """``identifier`` with each word replaced by ``word(word)``, in its style (None if a word
    has no replacement)."""
    parts = split_words(identifier)
    new = [word(part) for part in parts]
    if not parts or any(part is None for part in new):
        return None
    prefix = identifier[: len(identifier) - len(identifier.lstrip("_$"))]
    suffix = identifier[len(identifier.rstrip("_$")) :]
    return prefix + _style(identifier)([part for part in new if part is not None]) + suffix


def _pseudo_word(word: str, seed: int, attempt: int) -> str:
    digest = hashlib.sha256(f"{seed}:{word.lower()}:{attempt}".encode()).digest()
    syllables = max(2, min(4, (len(word) + 2) // 3))
    return "".join(
        _CONSONANTS[digest[2 * i] % len(_CONSONANTS)] + _VOWELS[digest[2 * i + 1] % len(_VOWELS)]
        for i in range(syllables)
    )


def _is_local_specifier(specifier: str) -> bool:
    """A TypeScript/JavaScript import of a file in the repository, not of a package."""
    return specifier.startswith(("./", "../", "/", "@/", "~/")) or specifier in {".", ".."}


@dataclass(frozen=True, slots=True)
class RenamePlan:
    seed: int
    tokens: dict[str, str]  # whole identifier → new identifier
    words: dict[str, str] = field(default_factory=dict[str, str])  # lowercase word → pseudo word
    # The code languages a name is renamed in (non-code files rename every name); a name
    # without an entry is renamed in all of them.
    scopes: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    kept: dict[str, str] = field(default_factory=dict[str, str])  # repository name → why kept

    def renames(self, name: str, languages: Collection[str] | None = None) -> bool:
        """Whether ``name`` is renamed in code of one of ``languages`` (None: anywhere)."""
        if name not in self.tokens:
            return False
        scope = self.scopes.get(name)
        return languages is None or scope is None or not scope.isdisjoint(languages)

    def sub(
        self, text: str, languages: Collection[str] | None = None, *, regex: bool = False
    ) -> str:
        """``text`` with every mapped whole identifier replaced; with ``languages``, only the
        names renamed in code of those languages. A ``regex`` reads escapes as escapes."""

        def replace(match: re.Match[str]) -> str:
            name = match[0]
            return self.tokens[name] if self.renames(name, languages) else name

        return (REGEX_TOKEN if regex else TOKEN).sub(replace, text)

    def _path_word(self, part: str) -> str:
        if part in self.tokens:
            return self.tokens[part]
        if part in PATH_KEPT or part.startswith("__") or not TOKEN.fullmatch(part):
            return part
        return _restyle(part, lambda word: self.words.get(word.lower())) or part

    def segment(self, segment: str) -> str:
        """One directory or file name: its stem renamed (a mapped name as mapped, otherwise
        word by word, kebab-case parts each in their own style), extensions kept."""
        if not segment or segment.startswith((".", "__")) or segment in PATH_KEPT:
            return segment
        if is_manifest(segment):  # docker-compose.yml, next.config.ts: found by their names
            return segment
        stem, dot, rest = segment.partition(".")
        if stem in self.tokens or stem in PATH_KEPT:
            renamed = self._path_word(stem)
        else:
            pieces = re.split(r"(-+)", stem)  # the separators stay at the odd indexes
            renamed = "".join(p if i % 2 else self._path_word(p) for i, p in enumerate(pieces))
        return renamed + dot + rest

    def path(self, path: str) -> str:
        return "/".join(self.segment(part) for part in PurePosixPath(path).parts)

    def specifier(self, specifier: str) -> str:
        """A relative (or ``@/``) import specifier, renamed like the path it points at."""
        target, mark, query = specifier.partition("?")
        return "/".join(self.segment(part) for part in target.split("/")) + mark + query


def _scoped(names: Names) -> dict[str, frozenset[str] | None]:
    if isinstance(names, Mapping):
        scoped = cast("Mapping[str, Iterable[str]]", names)
        return {name: frozenset(languages) for name, languages in scoped.items()}
    return dict.fromkeys(names)


def _why_kept(name: str) -> str | None:
    """Why ``name`` can't be renamed anywhere in code (None if it can)."""
    if name in PROTECTED:
        return KEPT_PROTECTED
    if name.startswith("__"):
        return "kept: a dunder name"
    if not TOKEN.fullmatch(name) or not split_words(name):
        return "kept: not an identifier"
    return None


def _stems(paths: Iterable[str]) -> frozenset[str]:
    """Every directory name and file stem in ``paths`` (``app/api/routes/items.py``: ``app``,
    ``api``, ``routes``, ``items``), and each kebab-case part (``changes-filter.yaml``:
    ``changes``, ``filter``): the names imports use, and the parts paths are renamed by."""
    return frozenset(
        part
        for path in paths
        for segment in PurePosixPath(path).parts
        for stem in [segment.partition(".")[0]]
        for part in {stem, *re.split(r"-+", stem)}
    )


def _path_parts(paths: Iterable[str], tokens: Mapping[str, str]) -> Iterator[str]:
    """The parts of directory and file names that :meth:`RenamePlan.segment` renames word by
    word (those that aren't mapped names or conventional)."""
    for path in sorted(set(paths)):
        for segment in PurePosixPath(path).parts:
            stem = segment.partition(".")[0]
            if segment.startswith((".", "__")) or segment in PATH_KEPT or stem in PATH_KEPT:
                continue
            if stem not in tokens:
                yield from (
                    part
                    for part in re.split(r"-+", stem)
                    if part not in tokens
                    and part not in PATH_KEPT
                    and not part.startswith("__")
                    and TOKEN.fullmatch(part)
                )


class _WordMap:
    """One pseudo word per word, never one already in the repository. Paths, and code names that
    are also a path's (``stems``), keep :data:`KEPT_WORDS`; other code names rename those too,
    except a hook's leading ``use``."""

    def __init__(self, seed: int, taken_lower: set[str], *, stems: frozenset[str]) -> None:
        self.seed = seed
        self.taken_lower = taken_lower
        self.stems = stems
        self.words: dict[str, str] = {}  # every word of paths and stems (kept words to themselves)
        self.role_words: dict[str, str] = {}  # kept words inside other code names

    def _pseudo(self, key: str) -> str:
        attempt = 0
        candidate = key
        while not key.isdigit() and (
            (candidate := _pseudo_word(key, self.seed, attempt)) in self.taken_lower
            or candidate in self.words.values()
            or candidate in self.role_words.values()
            or candidate in KEPT_WORDS
        ):
            attempt += 1
        return candidate

    def path_word(self, word: str) -> str:
        key = word.lower()
        if key in KEPT_WORDS:
            self.words[key] = key
        if key not in self.words:
            self.words[key] = self._pseudo(key)
        return self.words[key]

    def name_word(self, word: str) -> str:
        key = word.lower()
        if key not in KEPT_WORDS:
            return self.path_word(word)
        if key not in self.role_words:
            self.role_words[key] = self._pseudo(key)
        return self.role_words[key]

    def rename(self, name: str) -> str | None:
        """``name`` in its style with every word replaced (None if it has no words)."""
        if name in self.stems:
            return _restyle(name, self.path_word)
        if _HOOK.match(name):
            rest = _restyle(name[3:], self.name_word)
            return None if rest is None else "use" + rest
        return _restyle(name, self.name_word)


def make_plan(
    names: Names,
    existing: Iterable[str],
    *,
    seed: int = 0,
    external: Mapping[str, Iterable[str]] | None = None,
    paths: Iterable[str] = (),
) -> RenamePlan:
    """A consistent mapping for ``names`` (each with the languages defining it, or a plain list
    renamed in every language). New names never collide with ``existing`` tokens (all
    identifiers in the repository) or with each other. A name in ``external`` (language → names
    imported from libraries) isn't renamed in that language's code. Every word of the
    directory and file names in ``paths`` gets a pseudo word too, so paths are renamed whole."""
    paths = sorted(set(paths))
    taken = set(existing)
    taken_lower = {token.lower() for token in taken}
    imported = {language: set(found) for language, found in (external or {}).items()}
    tokens: dict[str, str] = {}
    scopes: dict[str, frozenset[str]] = {}
    kept: dict[str, str] = {}

    vocabulary = _WordMap(seed, taken_lower, stems=_stems(paths))
    words = vocabulary.words

    for name, languages in sorted(_scoped(names).items()):
        if reason := _why_kept(name):
            kept[name] = reason
            continue
        defined = CODE_LANGUAGES if languages is None else languages
        blocked = sorted(lang for lang in defined if name in imported.get(lang, ()))
        where = ", ".join(blocked)
        if blocked and not defined - set(blocked):
            kept[name] = f"kept in {where} code: imported from a library there"
            continue
        if blocked:
            kept[name] = f"not renamed in {where} code: imported from a library there"
        renamed = vocabulary.rename(name) or name
        if renamed == name:
            kept[name] = (
                "kept: a module, file or directory name made only of words the indexer reads a "
                "file's role from"
            )
            continue
        candidate, n = renamed, 2
        while candidate in taken or candidate in tokens.values():
            candidate = f"{renamed}{n}"
            n += 1
        tokens[name] = candidate
        if languages is not None or blocked:
            scopes[name] = frozenset(defined - set(blocked))
    for part in _path_parts(paths, tokens):
        for word in split_words(part):
            vocabulary.path_word(word)
    return RenamePlan(seed=seed, tokens=tokens, words=words, scopes=scopes, kept=kept)


# ---------------------------------------------------------------- python modules


@dataclass(frozen=True, slots=True)
class PythonModules:
    """The repository's Python modules, to tell its own imports from library ones. A directory
    without ``__init__.py`` is an import root (``backend/`` for ``import app``), so ``alembic``
    under the ``app`` package isn't mistaken for the alembic library."""

    paths: frozenset[PurePosixPath]  # module files without suffix, and directories with Python
    dotted: frozenset[str]  # names importable from some root

    @classmethod
    def of(cls, files: Iterable[str]) -> PythonModules:
        modules: set[PurePosixPath] = set()
        packages: set[PurePosixPath] = set()
        directories: set[PurePosixPath] = {PurePosixPath()}
        for file in files:
            path = PurePosixPath(file)
            if path.suffix not in PYTHON_SUFFIXES:
                continue
            if path.stem == "__init__":
                packages.add(path.parent)
            else:
                modules.add(path.with_suffix(""))
            directories.update(path.parents)
        roots = directories - packages
        everything = modules | (directories - {PurePosixPath()})
        dotted: set[str] = set()
        for module in everything:
            for root in module.parents:
                if root in roots:
                    dotted.add(".".join(module.relative_to(root).parts))
        return cls(paths=frozenset(everything), dotted=frozenset(dotted))

    def _relative(self, file: str, level: int, module: str | None) -> PurePosixPath:
        base = PurePosixPath(file).parent
        for _ in range(level - 1):
            base = base.parent
        return base.joinpath(*module.split(".")) if module else base

    def is_own(self, module: str | None, level: int) -> bool:
        return level > 0 or (module is not None and module in self.dotted)

    def is_submodule(self, file: str, module: str | None, level: int, name: str) -> bool:
        """Whether ``from <module> import <name>`` imports a module of the repository."""
        if level > 0:
            return self._relative(file, level, module) / name in self.paths
        return f"{module}.{name}" in self.dotted


_IMPORT_PART = re.compile(r"#[^\r\n]*|[^\W\d]\w*|\S")


def _import_edits(
    words: list[str],
    node: ast.Import | ast.ImportFrom,
    file: str,
    plan: RenamePlan,
    mods: PythonModules,
) -> dict[int, str]:
    """Replacements, by word index, for one import statement's words (as the tree reads them):
    the repository's module names renamed like their paths, imported names like their code."""
    py = (PYTHON,)
    edits: dict[int, str] = {}
    i = words.index("import")
    own_module = isinstance(node, ast.ImportFrom) and mods.is_own(node.module, node.level)
    if own_module:
        edits |= {j: plan.segment(words[j]) for j in range(1, i) if words[j] != "."}
    i += 1
    for alias in node.names:
        while words[i] in {"(", ")", ","}:
            i += 1
        names = list(range(i, i + 2 * len(alias.name.split(".")) - 1, 2))  # a.b.c: a, b, c
        i = names[-1] + 1
        asname = None
        if alias.asname is not None:
            asname, i = i + 1, i + 2
        if isinstance(node, ast.Import):
            if not mods.is_own(alias.name, 0):
                continue
            edits |= {j: plan.segment(words[j]) for j in names}
        elif not own_module or alias.name == "*":
            continue
        elif mods.is_submodule(file, node.module, node.level, alias.name):
            # A module renamed like its file; if its code name is kept, it stays bound to it.
            new, bound = plan.segment(alias.name), plan.sub(alias.name, py)
            edits[names[0]] = new if asname is not None or new == bound else f"{new} as {bound}"
        else:
            edits[names[0]] = plan.sub(alias.name, py)
        if asname is not None:
            edits[asname] = plan.sub(words[asname], py)
    return edits


def _rewrite_import(
    segment: str,
    node: ast.Import | ast.ImportFrom,
    file: str,
    plan: RenamePlan,
    mods: PythonModules,
) -> str:
    """One import statement's source, rewritten by :func:`_import_edits`."""
    # Comments and line continuations carry no names.
    parts = [m for m in _IMPORT_PART.finditer(segment) if m[0] != "\\" and m[0][0] != "#"]
    edits = _import_edits([m[0] for m in parts], node, file, plan, mods)
    out: list[str] = []
    position = 0
    for index, part in enumerate(parts):
        if index in edits:
            out += [segment[position : part.start()], edits[index]]
            position = part.end()
    return "".join(out) + segment[position:]


_NEWLINE = re.compile("\n")  # parse_python reads a lone CR as a space


def _python_import_edits(
    text: str, file: str, plan: RenamePlan, mods: PythonModules
) -> list[tuple[int, int, str]]:
    tree = parse_python(text)
    if tree is None:
        return []
    starts = [0, *(m.end() for m in _NEWLINE.finditer(text))]

    def offset(line: int, column: int) -> int:  # ast columns count UTF-8 bytes
        start = starts[line - 1]
        head = text[start : start + column].encode("utf-8")[:column]
        return start + len(head.decode("utf-8", errors="ignore"))

    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom) and node.end_lineno is not None:
            start = offset(node.lineno, node.col_offset)
            end = offset(node.end_lineno, node.end_col_offset or 0)
            edits.append((start, end, _rewrite_import(text[start:end], node, file, plan, mods)))
    return sorted(edits)


# ---------------------------------------------------------------- javascript imports

_JS_SPECIFIER = re.compile(
    r"""(?:\bfrom|\bimport|\bimport\s*\(|\brequire\s*\(|\b(?:vi|jest)\.mock\s*\()\s*"""
    r"""(?P<quote>["'])(?P<spec>[^"'\n]*)(?P=quote)"""
)
_JS_IMPORT_CLAUSE = re.compile(
    r"""\b(?:import|export)\s+(?P<clause>[\w$*\s{},]*?)\s*\bfrom\s*(?P<quote>["'])"""
    r"""(?P<spec>[^"'\n]+)(?P=quote)"""
    r"""|\b(?:const|let|var)\s+(?P<required>[\w${}\s,:]+?)\s*=\s*require\s*\(\s*"""
    r"""(?P<rquote>["'])(?P<rspec>[^"'\n]+)(?P=rquote)"""
)
_JS_CLAUSE_WORDS = frozenset({"as", "type", "typeof"})


def _js_specifier_edits(text: str, plan: RenamePlan) -> list[tuple[int, int, str]]:
    """Every import specifier: the repository's own renamed like their paths, packages kept
    (both left out of the identifier rewrite)."""
    edits: list[tuple[int, int, str]] = []
    for match in _JS_SPECIFIER.finditer(text):
        spec = match["spec"]
        new = plan.specifier(spec) if _is_local_specifier(spec) else spec
        edits.append((match.start("spec"), match.end("spec"), new))
    return edits


def _rewrite(text: str, edits: list[tuple[int, int, str]], rest: Callable[[str], str]) -> str:
    out: list[str] = []
    position = 0
    for start, end, new in edits:
        if start < position:  # pragma: no cover - statements and strings don't overlap
            continue
        out += [rest(text[position:start]), new]
        position = end
    return "".join(out) + rest(text[position:])


IGNORE_FILES = frozenset({".gitignore", ".senseiignore", ".dockerignore", ".prettierignore"})
ATTRIBUTES_FILE = ".gitattributes"


def _rename_patterns(text: str, plan: RenamePlan, *, attributes: bool) -> str:
    """An ignore or attributes file: each pattern renamed like the paths it matches (so the
    copy ignores what the original ignored), attribute names and comments left alone."""

    def pattern(line: str) -> str:
        body = line.removesuffix("\r")
        if not body.strip() or body.lstrip().startswith("#"):
            return line
        lead = body[: len(body) - len(body.lstrip())]
        # Attributes: the pattern is the first field. Ignore rules: the whole line, but for
        # trailing spaces (which git ignores unless escaped).
        split = re.fullmatch(r"(\S+)(.*)" if attributes else r"(.*?)(\s*)", body[len(lead) :])
        if split is None:  # pragma: no cover - both match any line that isn't blank
            return line
        target, rest = split[1], split[2]
        bang = "!" if target.startswith("!") else ""
        renamed = "/".join(plan.segment(part) for part in target.removeprefix("!").split("/"))
        return f"{lead}{bang}{renamed}{rest}{line[len(body) :]}"

    return "\n".join(pattern(line) for line in text.split("\n"))


def is_verbatim(path: str) -> bool:
    """A dependency manifest (``package.json``, ``pyproject.toml``, requirements, compose files,
    ``alembic.ini``): copied as it is. The indexer reads frameworks and package roots from its
    keys and library names; renamed, they would match nothing (the project's own name in it
    stays too)."""
    name = PurePosixPath(path).name
    return is_manifest(path) and language_of(path) is None and not name.startswith("openapi.")


def rename_text(text: str, path: str, plan: RenamePlan, mods: PythonModules) -> str:
    """The renamed content of the file at ``path``: code with the names of its language and its
    imports rewritten, a dependency manifest unchanged, other text with every name."""
    if is_verbatim(path):
        return text
    name = PurePosixPath(path).name
    if name in IGNORE_FILES:
        return _rename_patterns(text, plan, attributes=False)
    if name == ATTRIBUTES_FILE:
        return _rename_patterns(text, plan, attributes=True)
    language = language_of(path)
    if language is None:
        return plan.sub(text)
    if language == PYTHON:
        edits = _python_import_edits(text, path, plan, mods)
    else:
        edits = _js_specifier_edits(text, plan)
    return _rewrite(text, edits, lambda part: plan.sub(part, (language,)))


# ---------------------------------------------------------------- sources of names


def _indexed(checkout: Path) -> list[str]:
    files, _ = walk(checkout, IgnoreRules(()), max_files=MAX_FILES)
    return [file.path for file in files]


def _add_path_names(found: dict[str, set[str]], file: str, language: str | None) -> None:
    """A file's directory names (defined by the languages of the files under them; none for a
    directory without code) and, for code, its module file stem."""
    path = PurePosixPath(file)
    here = {language} if language else set[str]()
    for part in path.parent.parts:
        if not part.startswith(".") and TOKEN.fullmatch(part):
            found.setdefault(part, set()).update(here)
    stem = path.name.split(".")[0]
    if language is not None and TOKEN.fullmatch(stem):
        found.setdefault(stem, set()).add(language)


def labeling_symbols(checkout: Path) -> dict[str, frozenset[str]]:
    """Names the repository defines, with the code languages that define them, from the
    labeling parsers: definitions (each part of ``Class.method`` and ``Class.attr``), module
    file stems and directory names. Top-level declarations and class methods only, for
    TypeScript; :func:`extracted_symbols` finds every definition."""
    found: dict[str, set[str]] = {}
    for file in _indexed(checkout):
        language = language_of(file)
        _add_path_names(found, file, language)
        if language is not None and PurePosixPath(file).suffix in {".py", *TS_SUFFIXES}:
            for span in symbols_in(checkout, file):
                for part in span.name.split("."):
                    found.setdefault(part, set()).add(language)
    return {name: frozenset(languages) for name, languages in sorted(found.items())}


EXTRACT_TIMEOUT_SECONDS = 30.0  # per file; the indexer's 2 s would leave a slow file's names


def extracted_symbols(checkout: Path) -> dict[str, frozenset[str]]:
    """Names the repository defines, with the code languages that define them, from the
    tree-sitter symbol table the indexer builds: every definition it extracts (nested
    functions, methods, class attributes, React components, hooks and variables, wherever they
    are declared), plus module file stems and directory names as for
    :func:`labeling_symbols`. Keys that aren't identifiers (``'/login'``, ``200``) and the
    declarations in a ``declare module "<package>"`` block (they augment the library's own
    interfaces) are left out. A file that doesn't parse (too large, binary, timed out) adds
    only its path names."""
    found: dict[str, set[str]] = {}
    for file in _indexed(checkout):
        language = language_of(file)
        _add_path_names(found, file, language)
        try:
            data = read_source(checkout, file)
        except OSError:
            continue
        detected = detect_language(file, data[:256])
        if detected is None or not parseable(detected):
            continue
        result = parse_file(file, data, language=detected, timeout_seconds=EXTRACT_TIMEOUT_SECONDS)
        # A file rewritten as plain text (a script without a code suffix) renames every name.
        here = {language} if language else set[str]()
        for symbol in extract_symbols(result, detected):
            if symbol.kind is SymbolKind.MODULE or not TOKEN.fullmatch(symbol.name):
                continue
            if symbol.qualified_name.startswith(("'", '"')):
                continue  # inside `declare module "pkg"`: the library's names, augmented
            found.setdefault(symbol.name, set()).update(here)
    return {name: frozenset(languages) for name, languages in sorted(found.items())}


def external_names(checkout: Path) -> dict[str, frozenset[str]]:
    """Per language, the names the repository imports from libraries (and the attributes it
    reads off them): renaming those would break the library call."""
    files = _indexed(checkout)
    mods = PythonModules.of(files)
    python: set[str] = set()
    js: set[str] = set()
    for file in files:
        language = language_of(file)
        if language is None:
            continue
        try:
            text = (checkout / file).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if language == PYTHON:
            python |= _python_external(text, mods)
        else:
            js |= _js_external(text)
    return {PYTHON: frozenset(python), JS: frozenset(js)}


def _python_external(text: str, mods: PythonModules) -> set[str]:
    tree = parse_python(text)
    if tree is None:
        return set()
    names: set[str] = set()
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not mods.is_own(alias.name, 0):
                    bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not mods.is_own(node.module, node.level):
            for alias in node.names:
                names.add(alias.name)
                bound.add(alias.asname or alias.name)
    names |= bound
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in bound
        ):
            names.add(node.attr)
    return names - {"*"}


def _js_external(text: str) -> set[str]:
    names: set[str] = set()
    for match in _JS_IMPORT_CLAUSE.finditer(text):
        spec = match["spec"] or match["rspec"]
        if _is_local_specifier(spec):
            continue
        clause = match["clause"] if match["clause"] is not None else match["required"]
        names.update(name for name in TOKEN.findall(clause) if name not in _JS_CLAUSE_WORDS)
    if names:
        bindings = "|".join(re.escape(name) for name in sorted(names))
        names.update(re.findall(rf"(?<![\w$.])(?:{bindings})\??\.([A-Za-z_$][\w$]*)", text))
    return names


class NameSource(enum.StrEnum):
    EXTRACTED = "extracted"
    LABELING = "labeling"


NAME_SOURCES: dict[NameSource, SymbolSource] = {
    NameSource.EXTRACTED: extracted_symbols,
    NameSource.LABELING: labeling_symbols,
}


def symbols_from_file(path: Path) -> list[str]:
    """One name per line; blank lines and ``#`` comments are skipped."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def repository_tokens(checkout: Path) -> set[str]:
    tokens: set[str] = set()
    for file in _indexed(checkout):
        tokens.update(TOKEN.findall(file))
        try:
            tokens.update(TOKEN.findall((checkout / file).read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return tokens


def plan_for(checkout: Path, names: Names, *, seed: int = 0) -> RenamePlan:
    """The plan for a checkout: ``names`` renamed, library imports kept, paths renamed whole."""
    return make_plan(
        names,
        repository_tokens(checkout),
        seed=seed,
        external=external_names(checkout),
        paths=_indexed(checkout),
    )


# ---------------------------------------------------------------- the copy


@dataclass(slots=True)
class RenameResult:
    plan: RenamePlan
    paths: dict[str, str] = field(default_factory=dict[str, str])  # old → new, every file
    rewritten: int = 0  # text files whose content changed
    copied: int = 0  # files copied byte for byte (binary or unchanged)
    sha: str | None = None  # the copy's commit, once committed
    repo: str | None = None  # the copy's repository id
    # The repository, commit and suite it was made from: repo, sha, suite, suite_sha256.
    source: dict[str, str] = field(default_factory=dict[str, str])
    names: str | None = None  # where the renamed names came from (a NameSource, or a file)

    def mapping(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "source": self.source,
            "names": self.names,
            "seed": self.plan.seed,
            "sha": self.sha,
            "tokens": self.plan.tokens,
            "scopes": {name: sorted(scope) for name, scope in self.plan.scopes.items()},
            "words": self.plan.words,
            "kept": self.plan.kept,
            "paths": self.paths,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RenameResult:
        """The result a mapping file was written from (enough to translate gold again)."""
        scopes = cast("dict[str, list[str]]", data.get("scopes", {}))
        plan = RenamePlan(
            seed=int(data["seed"]),
            tokens=dict(data["tokens"]),
            words=dict(data.get("words", {})),
            scopes={name: frozenset(scope) for name, scope in scopes.items()},
            kept=dict(data.get("kept", {})),
        )
        return cls(
            plan=plan,
            paths=dict(data["paths"]),
            sha=data.get("sha"),
            repo=data.get("repo"),
            source=dict(data.get("source", {})),
            names=data.get("names"),
        )


def rename_tree(source: Path, target: Path, plan: RenamePlan) -> RenameResult:
    """Write the renamed copy of ``source`` (its files the indexer would keep, without ``.git``
    and symbolic links) to the new, empty directory ``target``."""
    if target.exists() and any(target.iterdir()):
        msg = f"{target} exists and is not empty"
        raise FileExistsError(msg)
    files = _indexed(source)
    mods = PythonModules.of(files)
    result = RenameResult(plan=plan)
    for file in files:
        new_path = plan.path(file)
        if new_path in result.paths.values():
            msg = f"two files would both become {new_path}"
            raise ValueError(msg)
        result.paths[file] = new_path
        destination = target / new_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = (source / file).read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            destination.write_bytes(raw)
            result.copied += 1
            continue
        renamed = rename_text(text, file, plan, mods)
        if renamed.count("\n") != text.count("\n"):  # pragma: no cover - no edit adds a line
            msg = f"renaming {file} changed its line count"
            raise AssertionError(msg)
        destination.write_bytes(renamed.encode("utf-8"))
        shutil.copymode(source / file, destination)
        if renamed != text:
            result.rewritten += 1
        else:
            result.copied += 1
    return result


def commit_tree(directory: Path) -> str:
    """Commit the copy as a fresh repository, so the same copy always gets the same sha on any
    machine: no global or system git config (no autocrlf, excludes or signing), a fixed author
    and date, and every file staged even if a ``.gitignore`` in the copy matches it. Every file
    is committed byte for byte, whatever the copy's ``.gitattributes`` say about line endings
    or filters, so a checkout of the commit is the copy."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
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
        "-c",
        "init.defaultBranch=main",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.safecrlf=false",
    ]

    def run(*args: str) -> None:
        subprocess.run([*git, *args], check=True, env=env, timeout=600)  # noqa: S603

    run("init", "-q")
    # info/attributes outranks every .gitattributes in the tree.
    info = directory / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text(
        "* -text !eol -filter -ident !working-tree-encoding\n", encoding="utf-8"
    )
    run("add", "-A", "-f")
    run("commit", "-q", "-m", "renamed copy")
    head = subprocess.run(  # noqa: S603
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
    )
    return head.stdout.strip()


def write_mapping(result: RenameResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.mapping(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- gold


class TranslateError(ValueError):
    """Gold that can't be pointed at the renamed copy."""


def _languages(paths: Iterable[str]) -> frozenset[str] | None:
    """The code languages of ``paths``; None (every name) if one is not code, or for none."""
    found = [language_of(path) for path in paths]
    if not found or None in found:
        return None
    return frozenset(language for language in found if language is not None)


def translate_target(target: GoldTarget, result: RenameResult) -> GoldTarget:
    path = result.paths.get(target.path)
    if path is None:
        msg = (
            f"gold path {target.path} is not in the renamed copy (the copy holds only the files "
            "the indexer keeps): fix the gold target"
        )
        raise TranslateError(msg)
    symbol = target.symbol
    if symbol is not None:
        symbol = result.plan.sub(symbol, _languages([target.path]))
    return target.model_copy(update={"path": path, "symbol": symbol})


_BACKTICKED = re.compile(r"`([^`]+)`")


def translate_query(
    query: str,
    query_class: QueryClass,
    plan: RenamePlan,
    languages: Collection[str] | None = None,
) -> str:
    """Symbol and regex queries quote the code, so all of them is renamed; the others are plain
    language written without identifiers, so only what they quote in backticks is. With
    ``languages`` (those of the gold), only names renamed in that code are."""
    if query_class is QueryClass.REGEX:
        return plan.sub(query, languages, regex=True)
    if query_class is QueryClass.SYMBOL:
        return plan.sub(query, languages)
    return _BACKTICKED.sub(lambda m: f"`{plan.sub(m[1], languages)}`", query)


def _translate_item(item: GoldenItem, result: RenameResult, new_repo: str, sha: str) -> GoldenItem:
    languages = _languages(target.path for target in item.gold)
    try:
        gold = [translate_target(target, result) for target in item.gold]
    except TranslateError as error:
        msg = f"item {item.id}: {error}"
        raise TranslateError(msg) from None
    return item.model_copy(
        update={
            "id": f"{item.id}.renamed",
            "repo": new_repo,
            "sha": sha,
            "query": translate_query(item.query, item.class_, result.plan, languages),
            "gold": gold,
        }
    )


def translate_suite(
    suite: Suite,
    repo: str,
    result: RenameResult,
    *,
    new_repo: str,
    source: RenamedFrom | None = None,
) -> Suite:
    """The suite's items for ``repo``, pointed at the renamed copy: paths, symbols and queries
    rewritten with the copy's mapping, lines unchanged, the sha the copy's commit. ``source``
    records where it came from. Raises :class:`TranslateError` for gold outside the copy."""
    if result.sha is None:
        msg = "commit the renamed copy before translating gold to it"
        raise ValueError(msg)
    sha = result.sha
    items = [_translate_item(item, result, new_repo, sha) for item in suite.for_repo(repo)]
    return Suite(
        name=f"{suite.name}-renamed", version=suite.version, items=items, renamed_from=source
    )


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
    document: dict[str, object] = {"name": suite.name, "version": suite.version}
    if suite.renamed_from is not None:
        document["renamed_from"] = suite.renamed_from.model_dump(mode="json")
    document["items"] = items
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


# ---------------------------------------------------------------- staleness


class _StaleError(Exception):
    """A renamed suite that can't even be compared with its source (the message says why)."""


def _staleness(suite_path: Path, suite: Suite, meta: RenamedFrom, checkouts: Path) -> list[str]:
    if len(suite.repos) != 1:
        msg = f"it covers {', '.join(suite.repos) or 'no repository'}, not one copy"
        raise _StaleError(msg)
    [new_repo] = suite.repos
    source_path = suite_path.parent / f"{meta.suite}.yaml"
    try:
        raw = source_path.read_bytes()
        source = parse_suite(raw.decode("utf-8"), str(source_path))
    except OSError:
        msg = f"its source suite {source_path} is missing"
        raise _StaleError(msg) from None
    except (SuiteError, UnicodeDecodeError):
        msg = f"its source suite {source_path} doesn't load (see its own errors)"
        raise _StaleError(msg) from None
    mapping_file = mapping_path(checkouts, new_repo)
    mapping = read_mapping(mapping_file)
    if mapping is None:
        msg = f"there is no mapping {mapping_file} for the copy"
        raise _StaleError(msg)
    reasons: list[str] = []
    made = cast("dict[str, Any]", mapping.get("source") or {})
    pinned = sorted({item.sha for item in source.for_repo(meta.repo)})
    if pinned != [made.get("sha")] or meta.sha != made.get("sha"):
        reasons.append(
            f"the copy was made from {meta.repo} at {made.get('sha')}, the source suite "
            f"{source_path.name} is pinned to {', '.join(pinned) or 'nothing'}"
        )
    if meta.sha256 != hashlib.sha256(raw).hexdigest():
        reasons.append(f"{source_path.name} changed since the copy was made (its sha256 differs)")
    if made.get("suite_sha256") != meta.sha256 or mapping.get("repo") not in {None, new_repo}:
        reasons.append(f"the mapping {mapping_file.name} comes from another rename")
    try:
        result = RenameResult.from_mapping(mapping)
        again = translate_suite(source, meta.repo, result, new_repo=new_repo, source=meta)
    except (KeyError, TypeError, ValueError) as error:
        return [*reasons, f"re-translating {source_path.name} fails ({error})"]
    if again != suite:
        ours = {item.id: item for item in suite.items}
        theirs = {item.id: item for item in again.items}
        differ = sorted(i for i in ours.keys() | theirs.keys() if ours.get(i) != theirs.get(i))
        where = f"items {', '.join(differ[:3])}" if differ else "the suite's name or version"
        reasons.append(
            f"re-translating {source_path.name} with the mapping doesn't reproduce it "
            f"({where} differ)"
        )
    return reasons


def check_renamed(
    name: str, suite_path: Path, suite: Suite, checkouts: Path, renamed: Collection[str]
) -> list[Problem]:
    """Errors for a renamed copy's suite that no longer matches what ``rename`` would write: a
    copy made from another commit than its source suite's, a source suite edited since, or
    gold that re-translating the source with the copy's mapping doesn't reproduce. ``renamed``
    are the ids of the renamed copies in ``checkouts``; the source suite is read from beside
    ``suite_path``."""
    meta = suite.renamed_from
    if meta is None:
        copies = [repo for repo in suite.repos if repo in renamed]
        if not copies:
            return []
        message = (
            f"a suite for the renamed copy {copies[0]} without `renamed_from` (written by an "
            "older rename, so it can't be checked for staleness): re-run rename"
        )
        return [Problem(name, f"repo {copies[0]}", message)]
    try:
        reasons = _staleness(suite_path, suite, meta, checkouts)
    except _StaleError as error:
        reasons = [str(error)]
    return [
        Problem(name, "renamed_from", f"stale renamed suite: {reason}: re-run rename")
        for reason in reasons
    ]
