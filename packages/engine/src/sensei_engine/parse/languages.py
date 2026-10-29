"""The grammars we parse with, and their queries.

A file's language id (from :mod:`sensei_engine.ingest.languages`) picks a grammar: ``python``,
``javascript`` (``.js`` and ``.jsx``: the JavaScript grammar reads JSX), ``typescript``, ``tsx``,
``java``, ``kotlin``, ``go``, ``csharp``, ``php`` (PHP with the HTML around it: a template's
``<?php`` blocks parse, its markup is text), ``ruby`` or ``rust``. Each grammar has one query
per :class:`QueryKind`, read from ``queries/<dir>/<kind>.scm``. A grammar's query is its own
directory's file followed by those of the directories it inherits (:data:`INHERITS`): TypeScript
and TSX share most patterns with JavaScript, and the JSX dialects share the component and
element patterns.

Query conventions (relied on by :func:`sensei_engine.parse.parser.parse_file`):

- every pattern has one *main* capture, named after the kind's family (``@definition.function``,
  ``@import.named``, ``@call``; see :data:`MAIN_CAPTURES`), on the node the fact is about;
- the other captures are its *roles* (``@name``, ``@source``, ``@arguments``);
- captures starting with ``_`` only serve predicates and are dropped;
- when two patterns capture the same node with the same roles, the first pattern wins, so
  specific patterns come first (a directory's own file comes before the ones it inherits).

Parsers aren't thread-safe: each thread gets its own (:func:`parser_for`). Languages and
compiled queries are immutable and shared.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources

import tree_sitter_c_sharp
import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_kotlin
import tree_sitter_php
import tree_sitter_python
import tree_sitter_ruby
import tree_sitter_rust
import tree_sitter_typescript
from tree_sitter import Language, Parser, Query


class QueryKind(enum.StrEnum):
    DEFS = "defs"  # functions, methods, classes, components, hooks, exports
    REFS = "refs"  # identifiers, attribute access
    IMPORTS = "imports"
    CALLS = "calls"
    JSX = "jsx"  # element names
    DECORATORS = "decorators"  # route decorators among them
    STRINGS = "strings"  # string literals with positions, for URL extraction


# The capture names a pattern's main capture can have, per kind: the name itself or a dotted
# refinement of it (``definition`` → ``definition.method``).
MAIN_CAPTURES: dict[QueryKind, tuple[str, ...]] = {
    QueryKind.DEFS: ("definition", "export"),
    QueryKind.REFS: ("reference",),
    QueryKind.IMPORTS: ("import",),
    QueryKind.CALLS: ("call",),
    QueryKind.JSX: ("jsx",),
    QueryKind.DECORATORS: ("decorator",),
    QueryKind.STRINGS: ("string",),
}

# Language id (ingest) to grammar.
GRAMMAR_BY_LANGUAGE: dict[str, str] = {
    "python": "python",
    "javascript": "javascript",
    "jsx": "javascript",
    "typescript": "typescript",
    "tsx": "tsx",
    "java": "java",
    "kotlin": "kotlin",
    "go": "go",
    "csharp": "csharp",
    "php": "php",
    "ruby": "ruby",
    "rust": "rust",
}

# Query directories each grammar inherits, nearest first (``ecma`` and ``jsx`` aren't grammars:
# they hold the patterns several grammars share).
INHERITS: dict[str, tuple[str, ...]] = {
    "python": (),
    "javascript": ("jsx", "ecma"),
    "typescript": ("ecma",),
    "tsx": ("jsx", "typescript"),
    "java": (),
    "kotlin": (),
    "go": (),
    "csharp": (),
    "php": (),
    "ruby": (),
    "rust": (),
    "jsx": (),
    "ecma": (),
}

_LANGUAGES: dict[str, Callable[[], object]] = {
    "python": tree_sitter_python.language,
    "javascript": tree_sitter_javascript.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "java": tree_sitter_java.language,
    "kotlin": tree_sitter_kotlin.language,
    "go": tree_sitter_go.language,
    "csharp": tree_sitter_c_sharp.language,
    "php": tree_sitter_php.language_php,
    "ruby": tree_sitter_ruby.language,
    "rust": tree_sitter_rust.language,
}

SUPPORTED_LANGUAGES = frozenset(GRAMMAR_BY_LANGUAGE)

# Comment node types: ``comment`` in Python, ECMAScript, Go, C#, PHP and Ruby; ``line_comment``
# and ``block_comment`` in Java, Kotlin and Rust (Javadoc, KDoc and Rust's ``///`` among them).
COMMENTS = frozenset({"comment", "line_comment", "block_comment"})


@dataclass(frozen=True, slots=True)
class Grammar:
    name: str
    language: Language
    queries: Mapping[QueryKind, Query]  # kinds with no patterns for the grammar are left out


def query_dirs(name: str) -> list[str]:
    """``name`` and the directories it inherits, depth first, each once."""
    order: list[str] = []

    def visit(directory: str) -> None:
        if directory in order:
            return
        order.append(directory)
        for parent in INHERITS[directory]:
            visit(parent)

    visit(name)
    return order


def query_source(name: str, kind: QueryKind) -> str:
    """The query text for grammar ``name``: its own patterns, then the inherited ones."""
    root = resources.files("sensei_engine.parse").joinpath("queries")
    parts: list[str] = []
    for directory in query_dirs(name):
        path = root.joinpath(directory, f"{kind.value}.scm")
        if path.is_file():
            parts.append(f"; ---- {directory}/{kind.value}.scm\n{path.read_text('utf-8')}")
    return "\n".join(parts)


@cache
def grammar(name: str) -> Grammar:
    """The grammar ``name`` with its compiled queries (a bad query raises ``QueryError``)."""
    language = Language(_LANGUAGES[name]())
    queries: dict[QueryKind, Query] = {}
    for kind in QueryKind:
        source = query_source(name, kind)
        if source.strip():
            queries[kind] = Query(language, source)
    return Grammar(name=name, language=language, queries=queries)


def grammar_for(language_id: str | None) -> Grammar | None:
    """The grammar for a file's language id, or None if we don't parse that language."""
    name = GRAMMAR_BY_LANGUAGE.get(language_id or "")
    return grammar(name) if name is not None else None


_local = threading.local()


def parser_for(grammar: Grammar) -> Parser:
    """This thread's parser for ``grammar``."""
    parsers: dict[str, Parser] | None = getattr(_local, "parsers", None)
    if parsers is None:
        parsers = _local.parsers = {}
    parser = parsers.get(grammar.name)
    if parser is None:
        parser = parsers[grammar.name] = Parser(grammar.language)
    return parser
