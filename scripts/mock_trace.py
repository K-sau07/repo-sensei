"""Write the web mocks' traces (apps/web/src/mocks/fixtures/trace.json).

The mock's trace view answers from the engine: this links fastapi/full-stack-fastapi-template at
b1125d8 (packages/engine/tests/fixtures/fastapi-template.tar.gz) in this process, as the graph
and links stages would (``sensei_evals.tracing.link_checkout``: no database, no sandbox), runs
3.12's ``trace_feature`` from the entries the mock serves and writes each result exactly as
``GET …/trace`` sends it (the router's own models and serializers), keyed by entry:

- the template's own traces (the Login page down to the user table, a dependency, a route);
- two that need what the template doesn't have, traced on a copy of the checkout with two small
  files added (:data:`SYNTHETIC`, each saying so): a call no endpoint matches (a gap, with the
  reason 3.10 found) and a call two endpoints match (an ambiguous hop);
- under ``unlinked``, the Login page traced before the links stage has run (the graph without its
  LINKS layer), as the API answers with ``linked: false``.

A break's ``detail`` is read where the API reads it, from the call's ``unmatched_reason``, here
from the links stage's build. The parse pool spawns workers, so this runs under ``__main__``::

    uv run python scripts/mock_trace.py
"""

from __future__ import annotations

import tarfile
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import orjson

from sensei_api.routers.trace import (
    TraceOut,
    _node,  # pyright: ignore[reportPrivateUsage]
    _path,  # pyright: ignore[reportPrivateUsage]
)
from sensei_engine.graph.project import Project
from sensei_engine.graph.snapshot import Edge, GraphSnapshot
from sensei_engine.graph.trace import trace_feature
from sensei_engine.link.stage import FileLinks, LinkBuild, resolve_links
from sensei_evals import tracing

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "packages" / "engine" / "tests" / "fixtures" / "fastapi-template.tar.gz"
OUT = ROOT / "apps" / "web" / "src" / "mocks" / "fixtures" / "trace.json"
SOURCE = "fastapi/full-stack-fastapi-template@b1125d8"
NOTE = (
    "GET /trace's answers for the mock snapshot, from the engine's trace_feature over the "
    "template linked in-process by scripts/mock_trace.py, keyed by entry (aliases: other ways "
    "to name the same entry). ItemsReport.tsx and reports.py are not in the template: the script "
    "adds them to its copy to show a gap and an ambiguous hop. 'unlinked' is traced before the "
    "links stage. Lines are 1-based and inclusive."
)

LOGIN = "tsx frontend/src/routes/login.tsx/Login()."
GET_DB = "py backend/app/api/deps.py/get_db()."
ADD_ITEM = "tsx frontend/src/components/Items/AddItem.tsx/AddItem()."
CREATE_ITEM = "POST /api/v1/items/"
EXPORT = "tsx frontend/src/components/Items/ItemsReport.tsx/ExportItems()."
REPORT = "tsx frontend/src/components/Items/ItemsReport.tsx/DailyReport()."
# The template's entries, and the other names each is known by (a file, a name).
TEMPLATE_ENTRIES: dict[str, tuple[str, ...]] = {
    LOGIN: ("frontend/src/routes/login.tsx", "Login"),
    GET_DB: (),
    ADD_ITEM: ("frontend/src/components/Items/AddItem.tsx",),
    CREATE_ITEM: (),
}
SYNTHETIC_ENTRIES: dict[str, tuple[str, ...]] = {EXPORT: ("ExportItems",), REPORT: ("DailyReport",)}

# Files added to the copy (path -> content), and lines appended to one of the template's (so its
# own lines keep their numbers). Each says what it is.
SYNTHETIC: dict[str, str] = {
    "frontend/src/components/Items/ItemsReport.tsx": """\
// Not in fastapi-template: scripts/mock_trace.py adds this to its copy of the checkout, for the
// trace view's mock: a call no endpoint matches (POST /items/export) and one that two match.
import { useMutation, useQuery } from "@tanstack/react-query"

const useExportItems = () =>
  useMutation({
    mutationFn: () => fetch("/api/v1/items/export", { method: "POST" }),
  })

export const ExportItems = () => {
  const mutation = useExportItems()
  return <button onClick={() => mutation.mutate()}>Export</button>
}

export const DailyReport = () => {
  const { data } = useQuery({
    queryKey: ["reports", "daily"],
    queryFn: () => fetch("/api/v1/reports/daily/latest").then((r) => r.json()),
  })
  return <pre>{JSON.stringify(data)}</pre>
}
""",
    "backend/app/api/routes/reports.py": '''\
"""Not in fastapi-template: scripts/mock_trace.py adds this to its copy of the checkout, for
the trace view's mock: two routes the same URL can mean (``/reports/daily/latest``)."""

from typing import Any

from fastapi import APIRouter
from sqlmodel import func, select

from app.api.deps import SessionDep
from app.models import Item

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{period}/latest")
def read_latest_report(session: SessionDep, period: str) -> Any:
    return session.exec(select(func.count()).select_from(Item)).one()


@router.get("/daily/{day}")
def read_daily_report(session: SessionDep, day: str) -> Any:
    return session.exec(select(Item)).all()
''',
}
APPENDED: dict[str, str] = {
    "backend/app/api/main.py": """

# Not in fastapi-template: scripts/mock_trace.py adds this to its copy: the reports routes.
from app.api.routes import reports  # noqa: E402

api_router.include_router(reports.router)
""",
}


def _checkout(base: Path, *, synthetic: bool) -> Path:
    with tarfile.open(TEMPLATE) as archive:
        archive.extractall(base, filter="data")
    root = base / "fastapi-template"
    if synthetic:
        for path, text in SYNTHETIC.items():
            (root / path).write_text(text)
        for path, text in APPENDED.items():
            with (root / path).open("a") as file:
                file.write(text)
    return root


def _linked(root: Path) -> tuple[GraphSnapshot, LinkBuild]:
    """``link_checkout``'s graph, and the links stage's build it made (whose call nodes carry
    3.10's reasons for matching nothing, which the API reads from the database)."""
    builds: list[LinkBuild] = []

    def keep(project: Project, files: Mapping[str, FileLinks]) -> LinkBuild:
        build = resolve_links(project, files)
        builds.append(build)
        return build

    with mock.patch.object(tracing, "resolve_links", keep):
        graph = tracing.link_checkout(root)
    [build] = builds
    return graph, build


def _without_links(graph: GraphSnapshot, build: LinkBuild) -> GraphSnapshot:
    """The graph before the links stage: its LINKS layer's nodes and edges left out."""
    dropped = {node.key for node in build.nodes}
    links = {(e.src, e.dst, e.type) for e in build.edges}
    keep = [i for i, node in enumerate(graph.nodes) if node.symbol_key not in dropped]
    at = {old: new for new, old in enumerate(keep)}
    edges: list[tuple[int, int, Edge]] = []
    for a, b, i in graph.graph.weighted_edge_list():
        edge = graph.edges[i]
        src, dst = graph.nodes[a].symbol_key, graph.nodes[b].symbol_key
        if a in at and b in at and (src, dst, edge.type) not in links:
            edges.append((at[a], at[b], edge))
    return GraphSnapshot([graph.nodes[i] for i in keep], edges)


def _reasons(build: LinkBuild) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in build.nodes:
        reason: Any = node.attrs.get("unmatched_reason")
        if isinstance(reason, str) and reason:
            found[node.key] = reason
    return found


def _trace(
    graph: GraphSnapshot, entry: str, details: dict[str, str], *, linked: bool
) -> dict[str, Any]:
    result = trace_feature(graph, entry)
    out = TraceOut(
        entries=[_node(graph, node) for node in result.entries],
        goal=_node(graph, result.goal) if result.goal is not None else None,
        linked=linked,
        paths=[_path(graph, path, details) for path in result.paths],
    )
    return out.model_dump(mode="json")


def _traces(
    graph: GraphSnapshot,
    entries: Mapping[str, tuple[str, ...]],
    trace: Callable[[GraphSnapshot, str], dict[str, Any]],
    aliases: dict[str, str],
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for entry, others in entries.items():
        found[entry] = trace(graph, entry)
        for other in others:
            if trace(graph, other) != found[entry]:
                msg = f"{other!r} doesn't trace as {entry!r}"
                raise AssertionError(msg)
            aliases[other] = entry
    return found


def build() -> dict[str, Any]:
    aliases: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as plain, tempfile.TemporaryDirectory() as added:
        graph, links = _linked(_checkout(Path(plain), synthetic=False))
        details = _reasons(links)
        traces = _traces(
            graph,
            TEMPLATE_ENTRIES,
            lambda g, entry: _trace(g, entry, details, linked=True),
            aliases,
        )
        unlinked = _traces(
            _without_links(graph, links),
            {LOGIN: TEMPLATE_ENTRIES[LOGIN]},
            lambda g, entry: _trace(g, entry, {}, linked=False),
            {},
        )
        more, more_links = _linked(_checkout(Path(added), synthetic=True))
        more_details = _reasons(more_links)
        traces |= _traces(
            more,
            SYNTHETIC_ENTRIES,
            lambda g, entry: _trace(g, entry, more_details, linked=True),
            aliases,
        )
    return {
        "source": SOURCE,
        "note": NOTE,
        "aliases": dict(sorted(aliases.items())),
        "traces": traces,
        "unlinked": unlinked,
    }


def main() -> None:
    OUT.write_bytes(orjson.dumps(build(), option=orjson.OPT_INDENT_2) + b"\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
