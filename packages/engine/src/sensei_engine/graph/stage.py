"""The graph stage (3.6.3): the snapshot's code graph, resolved from every file of a language we
have a resolver for and written at once (the CODE layer: imports, calls, references, bases, ...).

Each language's resolver is a module with ``LANGUAGES`` (the language ids it reads), ``extract``
(a parse result to the file's picklable facts, or None) and ``resolve`` (a :class:`Project` and
the facts by path to nodes, edges and stats): :data:`RESOLVERS`. Files are parsed again in the
pool and their facts extracted there (:func:`extract_files`: no tree leaves a worker process); the
project is read from the database (:func:`load_project`: its paths, the definitions the symbols
stage saved, and the config files that shape resolution, from the working copy); every resolver
runs over it (:func:`resolve_graph`), and the result replaces the snapshot's CODE layer in one
transaction, each module's import table kept on its node (:func:`write_graph`).

A file that doesn't parse, times out or makes a resolver's ``extract`` raise contributes nothing
and is counted; nothing a file holds stops the stage.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import SYMBOL_KINDS, CodeNode, RepoFile, Snapshot
from sensei_engine.chunk.store import SnapshotKey
from sensei_engine.graph import python
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.writer import CODE, GraphCounts, GraphEdge, GraphNode, GraphWriter
from sensei_engine.imports.ecma import graph as ecma
from sensei_engine.ingest.classify import MAX_PARSE_BYTES
from sensei_engine.parse.parser import TIMEOUT_SECONDS, ParseResult, parse_file
from sensei_engine.parse.pool import BATCH_SIZE, Failed, run_parallel
from sensei_engine.parse.stage import read_source

EXAMPLES = 10  # paths kept per problem, for the stage details
SECTIONS = ("calls", "code", "di", "tests")  # a resolver's stats noted as they are


class Resolver(Protocol):
    LANGUAGES: frozenset[str] | set[str]

    def extract(self, parsed: ParseResult) -> Any: ...

    def resolve(self, project: Project, facts: Mapping[str, Any]) -> tuple[Any, Any, Any]: ...


# The languages' resolvers, by name (a language a resolver doesn't list: no graph facts).
RESOLVERS: dict[str, ModuleType] = {"python": python, "ecma": ecma}


def resolver_for(language: str) -> str | None:
    return next((name for name, m in RESOLVERS.items() if language in m.LANGUAGES), None)


@dataclass(frozen=True, slots=True)
class GraphRequest:
    root: str
    path: str
    language: str
    resolver: str


@dataclass(frozen=True, slots=True)
class FileFacts:
    path: str
    resolver: str
    facts: Any = None  # None: nothing to resolve (didn't parse, or extract raised)
    failure: str | None = None


def extract_request(request: GraphRequest) -> FileFacts:
    """Parse one file and extract its graph facts (the pool's work, in a worker process)."""
    try:
        data = read_source(Path(request.root), request.path)
    except OSError as exc:
        return FileFacts(
            request.path, request.resolver, failure=f"unreadable: {type(exc).__name__}"
        )
    if len(data) > MAX_PARSE_BYTES:
        return FileFacts(request.path, request.resolver, failure="too_large")
    parsed = parse_file(request.path, data, language=request.language)
    if not parsed.ok:
        return FileFacts(request.path, request.resolver, failure=parsed.status.value)
    module = cast("Resolver", RESOLVERS[request.resolver])
    try:
        facts = module.extract(parsed)
    except Exception as exc:  # a bug for one file's shape loses its facts, not the stage
        return FileFacts(
            request.path, request.resolver, failure=f"{type(exc).__name__}: {exc}"[:300]
        )
    return FileFacts(request.path, request.resolver, facts)


def extract_files(
    root: Path,
    files: Sequence[tuple[str, str]],
    *,
    workers: int | None = None,
    batch_size: int = BATCH_SIZE,
) -> Iterator[FileFacts]:
    """The graph facts of ``files`` (``(path, language)`` pairs) under ``root``, as batches
    finish; files of languages no resolver reads are left out."""
    requests = [
        GraphRequest(str(root), path, language, name)
        for path, language in files
        if (name := resolver_for(language)) is not None
    ]
    for request, result in run_parallel(
        extract_request,
        requests,
        workers=workers,
        batch_size=batch_size,
        item_timeout=TIMEOUT_SECONDS,
    ):
        if isinstance(result, Failed):
            yield FileFacts(
                request.path, request.resolver, failure=f"{result.reason}: {result.message}"
            )
        else:
            yield result


def graph_languages(languages: Iterable[str | None]) -> bool:
    """Whether any of the languages has a resolver."""
    return any(language is not None and resolver_for(language) for language in languages)


# ---------------------------------------------------------------------- the project


async def load_project(db: AsyncSession, snapshot_id: Any, root: Path) -> Project:
    """The snapshot's files, the definitions the symbols stage saved (by path), and its config
    files' content (read from the working copy at ``root``; one that can't be read is left out)."""
    paths = list(await db.scalars(select(RepoFile.path).where(RepoFile.snapshot_id == snapshot_id)))
    rows = await db.execute(
        select(
            RepoFile.path,
            CodeNode.symbol_key,
            CodeNode.name,
            CodeNode.qualified_name,
            CodeNode.kind,
            CodeNode.start_line,
            CodeNode.end_line,
        )
        .join(RepoFile, RepoFile.id == CodeNode.file_id)
        .where(CodeNode.snapshot_id == snapshot_id, CodeNode.kind.in_(SYMBOL_KINDS))
        .order_by(RepoFile.path, CodeNode.start_line, CodeNode.symbol_key)
    )
    definitions: dict[str, list[Definition]] = {}
    for path, key, name, qualified, kind, start, end in rows:
        definitions.setdefault(path, []).append(
            Definition(key, name, qualified or name, kind, start, end)
        )
    configs: dict[str, str] = {}
    for path in paths:
        if is_config(path):
            try:
                configs[path] = read_source(root, path)[:MAX_PARSE_BYTES].decode("utf-8", "replace")
            except OSError:
                continue
    return Project(
        frozenset(paths),
        configs,
        {path: tuple(found) for path, found in definitions.items()},
    )


# ---------------------------------------------------------------------- resolution


@dataclass(slots=True)
class ExtractReport:
    files: int = 0
    extracted: int = 0
    failures: dict[str, int] = field(default_factory=dict[str, int])
    examples: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def add(self, file: FileFacts) -> None:
        self.files += 1
        if file.facts is not None:
            self.extracted += 1
            return
        reason = (file.failure or "no_facts").split(":", 1)[0]
        self.failures[reason] = self.failures.get(reason, 0) + 1
        paths = self.examples.setdefault(reason, [])
        if len(paths) < EXAMPLES:
            paths.append(file.path)


@dataclass(slots=True)
class GraphBuild:
    nodes: list[GraphNode] = field(default_factory=list[GraphNode])
    edges: list[GraphEdge] = field(default_factory=list[GraphEdge])
    tables: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    details: dict[str, Any] = field(default_factory=dict[str, Any])  # by resolver


def resolve_graph(
    project: Project,
    facts: Mapping[str, Mapping[str, Any]],
    *,
    clock: Callable[[], float] | None = None,
) -> GraphBuild:
    """Every resolver over the project (``facts``: by resolver, then path): nodes (one per key),
    edges, import tables and each resolver's stats."""
    now = clock or time.monotonic
    build = GraphBuild()
    nodes: dict[str, GraphNode] = {}
    for name, by_path in sorted(facts.items()):
        module = cast("Resolver", RESOLVERS[name])
        started = now()
        found_nodes, found_edges, stats = module.resolve(project, by_path)
        for node in found_nodes:
            nodes.setdefault(node.key, node)
        build.edges.extend(found_edges)
        build.tables.update(getattr(stats, "tables", None) or {})
        build.details[name] = {
            "files": len(by_path),
            "edges": len(found_edges),
            "seconds": round(now() - started, 3),
            **_stats(stats),
        }
    build.nodes = list(nodes.values())
    return build


def _stats(stats: object) -> dict[str, Any]:
    """A resolver's stats for the stage details: its imports' counts, and its calls' if any."""
    unresolved = list(getattr(stats, "unresolved", ()) or ())
    out: dict[str, Any] = {
        "imports": {
            "internal": int(getattr(stats, "internal", 0)),
            "resolved": int(getattr(stats, "resolved", 0)),
            "external": int(getattr(stats, "external", 0)),
            "unresolved": len(unresolved),
            "unresolved_examples": [list(u) for u in unresolved[:EXAMPLES]],
        }
    }
    for name in SECTIONS:  # a language's other counts: calls, code, dependencies, tests
        section = getattr(stats, name, None)
        if is_dataclass(section) and not isinstance(section, type):
            out[name] = {f.name: _plain(getattr(section, f.name)) for f in fields(section)}
    return out


def _plain(value: object) -> Any:
    """``value`` as JSON can hold it (long lists cut to a few examples)."""
    if isinstance(value, Mapping):
        items = cast("Mapping[object, object]", value).items()
        return {str(k): _plain(v) for k, v in sorted(items, key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple | set | frozenset):
        listed = list(cast("Iterable[object]", value))
        return [_plain(v) for v in listed[:EXAMPLES]]
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    return str(value)


async def write_graph(
    db: AsyncSession, snapshot: Snapshot | SnapshotKey, build: GraphBuild
) -> GraphCounts:
    """Replace the snapshot's CODE layer with ``build`` and keep each module's import table on
    its node, in the caller's transaction."""
    writer = GraphWriter(db, snapshot)
    counts = await writer.replace_all(build.nodes, build.edges, layer=CODE)
    await writer.annotate({key: {"imports": table} for key, table in build.tables.items()})
    return counts
