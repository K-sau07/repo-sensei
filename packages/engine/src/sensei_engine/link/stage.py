"""The links stage (3.12.4): the snapshot's cross-layer facts, from every linker at once (the LINKS
layer: endpoints and their handlers, frontend calls and the endpoints they target, models,
tables and the code that reads and writes them).

Like the graph stage, files are parsed again in the pool and their facts extracted there
(:func:`extract_links`: no tree leaves a worker process): a Python file's routes (3.8), imports
and data-layer facts (3.11), a JavaScript or TypeScript file's HTTP calls (3.9, with the ``.env``
values its folder sees). :func:`resolve_links` then joins them over the project (its paths,
definitions and configs, as :func:`sensei_engine.graph.stage.load_project` reads them):

- the routes, statically, reconciled with the running app's when the sandbox read them
  (:func:`runtime_routes`: the caller decides whether to run it);
- the client calls, and each matched to its endpoints through the URL trie (3.10), a call that
  matches none keeping why on its node;
- the models, tables and query sites;

and :func:`write_links` replaces the snapshot's LINKS layer with the result, in the caller's
transaction. A file that doesn't parse, or that a scanner can't read, contributes nothing and is
counted; nothing one file holds stops the stage.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from posixpath import basename
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import Snapshot
from sensei_engine.chunk.store import SnapshotKey
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.python.imports import PyFacts
from sensei_engine.graph.python.imports import extract as python_imports
from sensei_engine.graph.writer import LINKS, GraphCounts, GraphEdge, GraphNode, GraphWriter
from sensei_engine.ingest.classify import MAX_PARSE_BYTES
from sensei_engine.link import fastapi, http_calls, orm, urls
from sensei_engine.link.fastapi import FileRoutes, RouteGraph, RuntimeRoutes
from sensei_engine.link.http_calls import ECMA_LANGUAGES, ENV_FILES, FileCalls
from sensei_engine.link.orm import FileOrm
from sensei_engine.parse.parser import TIMEOUT_SECONDS, parse_file
from sensei_engine.parse.pool import BATCH_SIZE, Failed, run_parallel
from sensei_engine.parse.stage import read_source
from sensei_engine.sandbox.deps import DependencyImages
from sensei_engine.sandbox.runner import Sandbox
from sensei_engine.symbols.extract import extract_symbols

LANGUAGES = frozenset({"python", *ECMA_LANGUAGES})


def links_language(language: str | None) -> bool:
    """Whether a linker reads files of ``language``."""
    return language in LANGUAGES


# ---------------------------------------------------------------------- extraction


@dataclass(frozen=True, slots=True)
class LinkRequest:
    root: str
    path: str
    language: str
    env: Mapping[str, str] = field(default_factory=dict[str, str])  # what its folder's .env say


@dataclass(frozen=True, slots=True)
class FileLinks:
    """One file's facts for the linkers (picklable: they leave the pool)."""

    path: str
    routes: FileRoutes | None = None  # Python
    imports: PyFacts | None = None  # Python
    orm: FileOrm | None = None  # Python
    calls: FileCalls | None = None  # JavaScript and TypeScript
    failure: str | None = None  # why it has nothing (didn't parse, a scanner raised)


def extract_request(request: LinkRequest) -> FileLinks:
    """Parse one file and scan it for every linker of its language (the pool's work)."""
    try:
        data = read_source(Path(request.root), request.path)
    except OSError as exc:
        return FileLinks(request.path, failure=f"unreadable: {type(exc).__name__}")
    if len(data) > MAX_PARSE_BYTES:
        return FileLinks(request.path, failure="too_large")
    parsed = parse_file(request.path, data, language=request.language)
    if not parsed.ok:
        return FileLinks(request.path, failure=parsed.status.value)
    try:
        symbols = extract_symbols(parsed, request.language)
        definitions = [
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in symbols
        ]
        if request.language == "python":
            return FileLinks(
                request.path,
                routes=fastapi.scan_routes(parsed, definitions),
                imports=python_imports(parsed),
                orm=orm.scan(parsed, symbols),
            )
        return FileLinks(
            request.path, calls=http_calls.scan_calls(parsed, definitions, request.env)
        )
    except Exception as exc:  # a bug for one file's shape loses its facts, not the stage
        return FileLinks(request.path, failure=f"{type(exc).__name__}: {exc}"[:300])


def env_files(paths: Iterable[str]) -> list[str]:
    """The ``.env`` files among ``paths`` (their values become URL bases, 3.9)."""
    return sorted(p for p in paths if basename(p) in ENV_FILES)


def extract_links(
    root: Path,
    files: Sequence[tuple[str, str]],
    envs: Mapping[str, Mapping[str, str]],
    *,
    workers: int | None = None,
    batch_size: int = BATCH_SIZE,
) -> Iterator[FileLinks]:
    """The linkers' facts of ``files`` (``(path, language)`` pairs) under ``root``, as batches
    finish (``envs``: the ``.env`` values by folder, :func:`http_calls.env_values`); files of
    languages no linker reads are left out."""
    requests = [
        LinkRequest(
            str(root),
            path,
            language,
            http_calls.env_for(path, envs) if language in ECMA_LANGUAGES else {},
        )
        for path, language in files
        if links_language(language)
    ]
    for request, result in run_parallel(
        extract_request,
        requests,
        workers=workers,
        batch_size=batch_size,
        item_timeout=TIMEOUT_SECONDS,
    ):
        if isinstance(result, Failed):
            yield FileLinks(request.path, failure=f"{result.reason}: {result.message}")
        else:
            yield result


@dataclass(slots=True)
class LinkExtractReport:
    files: int = 0
    extracted: int = 0
    failures: dict[str, int] = field(default_factory=dict[str, int])
    examples: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def add(self, file: FileLinks, *, keep: int = 10) -> None:
        self.files += 1
        if file.failure is None:
            self.extracted += 1
            return
        problem = file.failure.split(":", 1)[0]
        self.failures[problem] = self.failures.get(problem, 0) + 1
        examples = self.examples.setdefault(problem, [])
        if len(examples) < keep:
            examples.append(file.path)


# ---------------------------------------------------------------------- resolution


@dataclass(slots=True)
class LinkBuild:
    nodes: list[GraphNode] = field(default_factory=list[GraphNode])
    edges: list[GraphEdge] = field(default_factory=list[GraphEdge])
    details: dict[str, Any] = field(default_factory=dict[str, Any])  # by linker: its report


def _python(files: Mapping[str, FileLinks]) -> tuple[dict[str, FileRoutes], dict[str, PyFacts]]:
    routes = {p: f.routes for p, f in files.items() if f.routes is not None}
    imports = {p: f.imports for p, f in files.items() if f.imports is not None}
    return routes, imports


async def runtime_routes(
    sandbox: Sandbox,
    images: DependencyImages,
    root: Path,
    project: Project,
    files: Mapping[str, FileLinks],
) -> RuntimeRoutes:
    """The running apps' routes (3.8), read in the sandbox from the snapshot at ``root``."""
    routes, imports = _python(files)
    entries = fastapi.app_entries(project, routes, imports)
    return await fastapi.runtime_routes(sandbox, images, root, project, entries)


def endpoints_of(graph: RouteGraph) -> list[urls.Endpoint]:
    """The endpoints 3.10 matches calls to, from 3.8's nodes."""
    return [
        urls.Endpoint(
            node.key,
            node.attrs["method"],
            node.attrs["path_norm"],
            node.attrs.get("operation_id"),
            node.attrs.get("root_path") or "",
        )
        for node in graph.nodes
    ]


def resolve_links(
    project: Project, files: Mapping[str, FileLinks], runtime: RuntimeRoutes | None = None
) -> LinkBuild:
    """Every linker over the project (``files``: each file's facts, by path; ``runtime``: the
    running app's routes, when the sandbox read them)."""
    routes, imports = _python(files)
    graph = fastapi.endpoint_facts(
        project, fastapi.static_routes(project, routes, imports), runtime
    )
    calls = http_calls.client_calls(
        project, {p: f.calls for p, f in files.items() if f.calls is not None}
    )
    linked = urls.link_calls(
        calls.calls, endpoints_of(graph), bases=urls.config_bases(project.configs)
    )
    data = orm.link(f.orm for _, f in sorted(files.items()) if f.orm is not None)
    build = LinkBuild()
    build.nodes = [*graph.nodes, *urls.with_reasons(calls.nodes, linked.report), *data.nodes]
    build.edges = [*graph.edges, *calls.edges, *linked.edges, *data.edges]
    build.details = {
        "routes": asdict(graph.report),
        "calls": asdict(calls.report),
        "urls": asdict(linked.report),
        "orm": asdict(data.report),
    }
    return build


async def write_links(
    db: AsyncSession, snapshot: Snapshot | SnapshotKey, build: LinkBuild
) -> GraphCounts:
    """Replace the snapshot's LINKS layer with ``build``, in the caller's transaction."""
    return await GraphWriter(db, snapshot).replace_all(build.nodes, build.edges, layer=LINKS)
