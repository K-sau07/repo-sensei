"""Calls, React structure and tests of JavaScript and TypeScript files, as graph edges (3.5),
from what :mod:`sensei_engine.imports.ecma.calls` extracted and the import tables
(:mod:`sensei_engine.imports.ecma.table`).

A called name is bound in this order (engine §11.2's tiers):

1. **Scope and import table** (``import_resolved``, 0.9): a definition around the call or at the
   top of the file (``helper()``, a nested ``onSubmit()``); an imported name, through barrels; a
   name in a namespace import (``api.listItems()``); a static member of a class, or a method of an
   object literal, defined here or imported (``Store.create()``, ``api.list()`` for
   ``const api = { list() {} }``).
2. **The receiver's type** (``receiver_heuristic``, 0.7): ``this.m()`` is the enclosing class's
   ``m``.
3. **The name alone**, only for a method on a receiver of unknown type (``item.archive()``): the
   one class method of that name in the repository (``name_unique``, 0.4), else each of up to 5
   (``name_ambiguous``, 0.2/k); more are dropped. A name the platform's objects have (``map``,
   ``then``, ``read``, ``start``: ``js_builtin_methods.txt``) is never guessed, nor a method of
   an object literal (a test's mock).

A plain name that is neither in scope nor imported is a global (``fetch``, ``setTimeout``) or a
local variable: no edge. A name imported from a package goes to its ``external_symbol`` node.

Edges, from the definition the call is made in (the module's key at the top of a file):

- ``CALLS``; a call of a hook (``use`` + a capital) is ``USES_HOOK`` instead;
- ``RENDERS``: a JSX element (``<Button>``, ``<Layout.Main>``) to its component;
- ``REFERENCES``: a prop given a function (``onClick={handleClick}``, ``{this.renderRow}``);
- in a test block (``it("…")``, ``test("…")``): a ``test`` node
  (``test <path>:<line>``, named by the title), with ``TESTS`` edges to the repository's
  definitions it calls or renders (at most 0.7: being called in a test is a heuristic), instead
  of the calls from the module.

Each edge's evidence is the file and line of the call; edges and test nodes are owned by the file.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from importlib import resources

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.imports.ecma.calls import CallFact, FileCode, JsxFact
from sensei_engine.imports.ecma.statements import DEFAULT
from sensei_engine.imports.ecma.table import ImportBinding, ImportTables, TargetKind
from sensei_engine.symbols.keys import escape

MAX_CANDIDATES = 5  # name_ambiguous fans out to at most this many definitions
TESTS_CONFIDENCE = 0.7
_HOOK = re.compile(r"^use[A-Z0-9]")
_CALLABLE_KINDS = frozenset({"function", "method", "component", "hook"})


@cache
def builtin_methods() -> frozenset[str]:
    text = resources.files(__package__).joinpath("js_builtin_methods.txt").read_text("utf-8")
    return frozenset(
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    )


@dataclass(frozen=True, slots=True)
class Meaning:
    """What a name means: a node key, and how that is known."""

    key: str
    provenance: Provenance
    candidates: int = 1
    external: tuple[str, str] | None = None  # (package, name) for a package's export

    @property
    def confidence(self) -> float:
        return confidence_of(self.provenance, self.candidates)


@dataclass(slots=True)
class CodeStats:
    calls: int = 0  # calls, elements and callbacks looked at
    resolved: Counter[str] = field(default_factory=Counter[str])  # by provenance
    external: int = 0
    tests: int = 0


class CodeResolver:
    """Binds the names a project's files call and render (see the module)."""

    def __init__(self, project: Project, tables: ImportTables) -> None:
        self.project = project
        self.tables = tables
        self._keys: dict[str, frozenset[str]] = {}
        self._kinds: dict[str, str] = {}
        methods: dict[str, list[str]] = {}
        for path in tables.files:
            definitions: Iterable[Definition] = project.definitions.get(path, ())
            self._keys[path] = frozenset(d.symbol_key for d in definitions)
            for d in project.definitions.get(path, ()):
                self._kinds[d.symbol_key] = d.kind
                if d.kind == "method" and d.symbol_key.removesuffix(
                    f"{escape(d.name)}()."
                ).endswith("#"):  # a class's, not an object literal's (a mock's)
                    methods.setdefault(d.name, []).append(d.symbol_key)
        self._methods = methods
        self._tables: dict[str, dict[str, ImportBinding]] = {}

    def table(self, path: str) -> dict[str, ImportBinding]:
        found = self._tables.get(path)
        if found is None:
            found = self._tables[path] = self.tables.table(path)
        return found

    # ------------------------------------------------------------------ names

    def _scopes(self, path: str, caller: str) -> list[str]:
        """The keys a name may be defined under, innermost first: the caller, the definitions
        around it, the module."""
        keys = self._keys.get(path, frozenset())
        around = [k for k in keys if caller.startswith(k) and k != caller]
        return [caller, *sorted(around, key=len, reverse=True)]

    def _in_scope(self, path: str, caller: str, name: str, *, new: bool) -> str | None:
        keys = self._keys.get(path, frozenset())
        own = escape(name)
        suffixes = ("#", "().", ".") if new else ("().", "#", ".")
        for scope in self._scopes(path, caller):
            for suffix in suffixes:
                key = f"{scope}{own}{suffix}"
                if key in keys:
                    return key
        return None

    def _imported(self, path: str, name: str) -> Meaning | None:
        binding = self.table(path).get(name)
        if binding is None:
            return None
        target = binding.target
        if target.kind is TargetKind.SYMBOL and target.symbol_key:
            return Meaning(target.symbol_key, Provenance.IMPORT_RESOLVED)
        if target.kind is TargetKind.EXTERNAL and target.package and target.name:
            return _external(target.package, target.name)
        return None

    def plain(self, path: str, caller: str, name: str, *, new: bool = False) -> Meaning | None:
        """``name`` used on its own: in scope, else imported (else a global: None)."""
        key = self._in_scope(path, caller, name, new=new)
        if key is not None:
            return Meaning(key, Provenance.IMPORT_RESOLVED)
        return self._imported(path, name)

    def _bound(self, path: str, caller: str, name: str) -> bool:
        return self._in_scope(path, caller, name, new=False) is not None or name in self.table(path)

    def _this(self, path: str, caller: str, name: str) -> Meaning | None:
        keys = self._keys.get(path, frozenset())
        for scope in self._scopes(path, caller):
            if scope.endswith("#"):
                key = f"{scope}{escape(name)}()."
                if key in keys:
                    return Meaning(key, Provenance.RECEIVER_HEURISTIC)
                return None
        return None

    def _through(self, path: str, caller: str, receiver: str, name: str) -> Meaning | None:
        """``ns.f`` through a namespace import, ``Cls.f`` on a class defined or imported."""
        binding = self.table(path).get(receiver)
        if binding is None:
            local = self._in_scope(path, caller, receiver, new=True)
            return self._static(path, local, name) if local is not None else None
        target = binding.target
        if target.kind is TargetKind.MODULE and target.path:
            found = self.tables.export_of(target.path, name)
            key = found.symbol_key if found is not None else None
            return Meaning(key, Provenance.IMPORT_RESOLVED) if key else None
        if target.kind is TargetKind.EXTERNAL and target.package:
            member = name if target.name in (None, DEFAULT) else f"{target.name}.{name}"
            return _external(target.package, member)
        if target.kind is TargetKind.SYMBOL and target.symbol_key and target.path:
            return self._static(target.path, target.symbol_key, name)
        return None

    def _static(self, path: str, owner: str, name: str) -> Meaning | None:
        """``owner.name()``: a class's static method, or an object literal's method
        (``const api = { list() {} }`` keys ``api.list().``)."""
        if not owner.endswith(("#", ".")) or owner.endswith("()."):
            return None
        key = f"{owner}{escape(name)}()."
        if key in self._keys.get(path, frozenset()):
            return Meaning(key, Provenance.IMPORT_RESOLVED)
        return None

    def by_name_all(self, name: str) -> list[Meaning]:
        if name in builtin_methods():
            return []
        keys = sorted(self._methods.get(name, ()))
        if not keys or len(keys) > MAX_CANDIDATES:
            return []
        if len(keys) == 1:
            return [Meaning(keys[0], Provenance.NAME_UNIQUE)]
        return [Meaning(k, Provenance.NAME_AMBIGUOUS, len(keys)) for k in keys]

    def call(self, path: str, fact: CallFact) -> list[Meaning]:
        """What ``fact`` calls: one meaning, several (a name that is ambiguous), or none."""
        receiver = fact.receiver
        if receiver is None:
            found = self.plain(path, fact.caller, fact.name, new=fact.new)
            return [found] if found is not None else []
        if receiver == "super":
            return []
        if receiver == "this":
            found = self._this(path, fact.caller, fact.name)
            return [found] if found is not None else []
        head = receiver.split(".", 1)[0]
        if receiver and "." not in receiver:
            found = self._through(path, fact.caller, receiver, fact.name)
            if found is not None:
                return [found]
        if head and head != "this" and self._bound(path, fact.caller, head):
            return []  # a value we know, whose members aren't definitions (an object)
        return self.by_name_all(fact.name)  # a receiver of unknown type

    def element(self, path: str, fact: JsxFact) -> Meaning | None:
        receiver, _, name = fact.name.rpartition(".")
        if not receiver:
            return self.plain(path, fact.caller, name)
        if "." in receiver:
            return None
        return self._through(path, fact.caller, receiver, name)

    def callback(self, path: str, caller: str, value: str) -> Meaning | None:
        receiver, _, name = value.rpartition(".")
        if not receiver:
            found = self.plain(path, caller, name)
        elif receiver == "this":
            found = self._this(path, caller, name)
        else:
            return None
        if found is None or found.external is not None:
            return None
        return found if self._kinds.get(found.key) in _CALLABLE_KINDS else None

    def kind(self, key: str) -> str | None:
        return self._kinds.get(key)


def _external(package: str, name: str) -> Meaning:
    return Meaning(
        f"external_symbol npm:{package}/{name}",
        Provenance.IMPORT_RESOLVED,
        external=(package, name),
    )


def _external_node(nodes: dict[str, GraphNode], key: str, package: str, name: str) -> None:
    if key not in nodes:
        nodes[key] = GraphNode(
            key=key,
            kind=NodeKind.EXTERNAL_SYMBOL,
            name=package if name == DEFAULT else name,
            qualified_name=f"{package}.{name}",
            lang="javascript",
            attrs={"package": package},
        )


def _test_key(path: str, line: int) -> str:
    return f"test {path}:{line}"


def code_edges(
    resolver: CodeResolver,
    path: str,
    code: FileCode,
    nodes: dict[str, GraphNode],
    stats: CodeStats,
) -> list[GraphEdge]:
    """``path``'s CALLS, USES_HOOK, RENDERS, REFERENCES and TESTS edges (see the module); the
    test nodes and the external nodes they reach are added to ``nodes``."""
    edges: list[GraphEdge] = []
    tests = [_test_key(path, test.line) for test in code.tests]
    for test, key in zip(code.tests, tests, strict=True):
        stats.tests += 1
        nodes[key] = GraphNode(
            key=key,
            kind=NodeKind.TEST,
            name=test.title,
            path=path,
            start_line=test.line,
            end_line=test.end_line,
            qualified_name=test.title,
            lang="javascript",
            owner=path,
        )

    def add(src: str, meaning: Meaning, type_: EdgeType, line: int, test: int | None) -> None:
        if test is not None and meaning.external is not None:
            return  # a test calling a package (expect, render): not what it tests
        if meaning.external is not None:
            stats.external += 1
            _external_node(nodes, meaning.key, *meaning.external)
        else:
            stats.resolved[meaning.provenance.value] += 1
        if test is not None:
            confidence = min(meaning.confidence, TESTS_CONFIDENCE)
            edges.append(
                _edge(
                    tests[test],
                    meaning,
                    EdgeType.TESTS,
                    path=path,
                    line=line,
                    confidence=confidence,
                )
            )
        else:
            edges.append(
                _edge(src, meaning, type_, path=path, line=line, confidence=meaning.confidence)
            )

    for fact in code.calls:
        stats.calls += 1
        type_ = EdgeType.USES_HOOK if _HOOK.match(fact.name) else EdgeType.CALLS
        for meaning in resolver.call(path, fact):
            add(fact.caller, meaning, type_, fact.line, fact.test)
    for element in code.elements:
        stats.calls += 1
        meaning = resolver.element(path, element)
        if meaning is not None:
            add(element.caller, meaning, EdgeType.RENDERS, element.line, element.test)
    for callback in code.callbacks:
        stats.calls += 1
        meaning = resolver.callback(path, callback.caller, callback.value)
        if meaning is not None:
            add(callback.caller, meaning, EdgeType.REFERENCES, callback.line, None)
    return edges


def _edge(
    src: str, meaning: Meaning, type_: EdgeType, *, path: str, line: int, confidence: float
) -> GraphEdge:
    default = confidence_of(meaning.provenance, meaning.candidates)
    return GraphEdge(
        src=src,
        dst=meaning.key,
        type=type_,
        provenance=meaning.provenance,
        confidence=None if confidence == default else confidence,
        evidence_path=path,
        evidence_line=line,
        owner=path,
    )
