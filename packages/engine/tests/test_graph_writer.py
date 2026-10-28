"""The graph (3.1): confidence by provenance, and the writer: facts by key, owned by files (a file
re-written leaves nothing behind), one edge per evidence line, org-scoped, and definitions left
to the symbols stage (which, run again, leaves the graph alone), and two layers that never take
each other's facts."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    GRAPH_KINDS,
    CodeEdge,
    CodeNode,
    EdgeType,
    NodeKind,
    Provenance,
    Repo,
    RepoSource,
    Snapshot,
)
from sensei_core.orgs.service import OrgService
from sensei_engine.graph.confidence import FLAGGED, confidence_of, path_confidence
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.graph.writer import (
    CODE,
    LINKS,
    GraphEdge,
    GraphError,
    GraphNode,
    GraphWriter,
)
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.store import file_ids, save_discovery
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols
from sensei_engine.symbols.lookup import find_symbols, symbols_at
from sensei_engine.symbols.store import FileSymbols, save_symbols
from sensei_testing import make_user

pytestmark = pytest.mark.integration

FILES = {
    "app/util.py": "def helper():\n    return 1\n",
    "app/service.py": "from app.util import helper\n\n\ndef create_item():\n    return helper()\n",
    "web/client.ts": "export async function createItem() {\n  return fetch('/api/items')\n}\n",
}
HELPER = "py app/util.py/helper()."
CREATE = "py app/service.py/create_item()."
CALL = "http_client_call POST /api/items/ web/client.ts:2"
ENDPOINT = "api_endpoint POST /api/items/"


# ---------------------------------------------------------------------- confidence


def test_confidence_follows_provenance() -> None:
    assert confidence_of(Provenance.SCIP) == confidence_of(Provenance.RUNTIME) == 1.0
    assert confidence_of(Provenance.IMPORT_RESOLVED) == 0.9
    assert confidence_of(Provenance.NAME_UNIQUE) == 0.4
    assert confidence_of(Provenance.NAME_AMBIGUOUS, candidates=4) == pytest.approx(0.05)
    assert confidence_of(Provenance.NAME_UNIQUE, candidates=4) == 0.4  # only ambiguity divides
    assert {Provenance.LLM_INFERRED} == FLAGGED
    assert path_confidence([0.9, 0.5, 1.0]) == pytest.approx(0.45)
    assert path_confidence([]) == 1.0
    with pytest.raises(ValueError, match="at least 1"):
        confidence_of(Provenance.NAME_AMBIGUOUS, candidates=0)


def test_every_edge_type_and_graph_kind_is_one_layers() -> None:
    assert CODE.edge_types | LINKS.edge_types == set(EdgeType)
    assert not CODE.edge_types & LINKS.edge_types
    assert CODE.node_kinds | LINKS.node_kinds == set(GRAPH_KINDS)
    assert not CODE.node_kinds & LINKS.node_kinds


# ---------------------------------------------------------------------- the writer


async def _snapshot(db: AsyncSession, root: Path) -> Snapshot:
    for path, content in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=12,
        owner="o",
        name="graph",
        full_name="o/graph",
        slug="graph",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="main", commit_sha="c" * 40)
    db.add(snapshot)
    await db.flush()
    await save_discovery(db, snapshot, discover(root, max_files=100))
    await _symbols(db, snapshot)
    return snapshot


async def _symbols(db: AsyncSession, snapshot: Snapshot) -> None:
    found: list[FileSymbols] = []
    for path, content in FILES.items():
        parsed = parse_file(path, content.encode())
        assert parsed.ok
        assert parsed.language is not None
        found.append(FileSymbols(path, parsed.language, extract_symbols(parsed, parsed.language)))
    await save_symbols(db, snapshot, await file_ids(db, snapshot.id), found)


async def _nodes(db: AsyncSession, snapshot: Snapshot) -> dict[str, uuid.UUID]:
    rows = await db.execute(
        select(CodeNode.symbol_key, CodeNode.id).where(
            CodeNode.snapshot_id == snapshot.id, CodeNode.kind.not_in(["module", "variable"])
        )
    )
    return dict(rows.tuples().all())


async def _edges(db: AsyncSession, snapshot: Snapshot) -> set[tuple[str, str, str, float, str]]:
    src, dst = CodeNode.__table__.alias("src"), CodeNode.__table__.alias("dst")
    rows = await db.execute(
        select(src.c.symbol_key, dst.c.symbol_key, CodeEdge.type, CodeEdge.confidence)
        .add_columns(CodeEdge.provenance)
        .join(src, src.c.id == CodeEdge.src_id)
        .join(dst, dst.c.id == CodeEdge.dst_id)
        .where(CodeEdge.snapshot_id == snapshot.id)
    )
    return {(a, b, t.value, round(c, 3), p.value) for a, b, t, c, p in rows.tuples()}


def _client_facts() -> tuple[list[GraphNode], list[GraphEdge]]:
    call = GraphNode(
        CALL,
        NodeKind.HTTP_CLIENT_CALL,
        "POST /api/items/",
        path="web/client.ts",
        start_line=2,
        end_line=2,
        lang="typescript",
        attrs={"method": "POST", "url": "/api/items"},
    )
    issues = GraphEdge(
        "ts web/client.ts/createItem().",
        CALL,
        EdgeType.ISSUES_REQUEST,
        Provenance.RECEIVER_HEURISTIC,
        evidence_path="web/client.ts",
        evidence_line=2,
    )
    return [call], [issues]


async def test_a_files_facts_are_written_by_key(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    writer = GraphWriter(db_session, snapshot)
    counts = await writer.replace_file(
        "app/service.py",
        [],
        [
            GraphEdge(
                CREATE,
                HELPER,
                EdgeType.CALLS,
                Provenance.IMPORT_RESOLVED,
                evidence_path="app/service.py",
                evidence_line=5,
            ),
            GraphEdge(CREATE, "py app/nowhere.py/gone().", EdgeType.CALLS, Provenance.NAME_UNIQUE),
        ],
        layer=CODE,
    )
    assert (counts.nodes, counts.edges, counts.unresolved) == (0, 1, 1)
    nodes, edges = _client_facts()
    counts = await writer.replace_file("web/client.ts", nodes, edges, layer=LINKS)
    assert (counts.nodes, counts.edges, counts.unresolved) == (1, 1, 0)
    assert await _edges(db_session, snapshot) == {
        (CREATE, HELPER, "calls", 0.9, "import_resolved"),
        ("ts web/client.ts/createItem().", CALL, "issues_request", 0.7, "receiver_heuristic"),
    }
    call = await db_session.scalar(select(CodeNode).where(CodeNode.symbol_key == CALL))
    assert call is not None
    assert (call.kind, call.start_line, call.attrs["method"]) == ("http_client_call", 2, "POST")
    assert call.file_id is not None
    assert call.file_id == call.owner_file_id
    edge = await db_session.scalar(
        select(CodeEdge).where(CodeEdge.type == EdgeType.CALLS, CodeEdge.snapshot_id == snapshot.id)
    )
    assert edge is not None
    assert (edge.evidence_line, edge.evidence_file_id is not None) == (5, True)


async def test_rewriting_a_file_leaves_nothing_of_it_behind(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    writer = GraphWriter(db_session, snapshot)
    nodes, edges = _client_facts()
    await writer.replace_file("web/client.ts", nodes, edges, layer=LINKS)
    # A snapshot-wide fact: the call matched to an endpoint (owned by no file).
    endpoint = GraphNode(ENDPOINT, NodeKind.API_ENDPOINT, "POST /api/items/")
    await writer.replace_all(
        [*nodes, endpoint],
        [*edges, GraphEdge(CALL, ENDPOINT, EdgeType.TARGETS, Provenance.URL_LITERAL)],
        layer=LINKS,
    )
    first = await _nodes(db_session, snapshot)
    assert len(await _edges(db_session, snapshot)) == 2

    # The file again, the same call: its node keeps its id, and the edge into it survives.
    await writer.replace_file("web/client.ts", nodes, edges, layer=LINKS)
    assert (await _nodes(db_session, snapshot))[CALL] == first[CALL]
    assert len(await _edges(db_session, snapshot)) == 2

    # The file without the call: its node goes, with every edge at either end.
    await writer.replace_file("web/client.ts", [], [], layer=LINKS)
    assert CALL not in await _nodes(db_session, snapshot)
    assert await _edges(db_session, snapshot) == set()
    left = await db_session.scalar(
        select(func.count()).select_from(CodeEdge).where(CodeEdge.snapshot_id == snapshot.id)
    )
    assert left == 0
    assert ENDPOINT in await _nodes(db_session, snapshot)  # owned by no file: still there


async def test_one_edge_per_evidence_line_and_the_more_certain_stays(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    writer = GraphWriter(db_session, snapshot)
    guess = GraphEdge(CREATE, HELPER, EdgeType.CALLS, Provenance.NAME_UNIQUE)  # no line
    sure = GraphEdge(CREATE, HELPER, EdgeType.CALLS, Provenance.SCIP)
    counts = await writer.replace_all([], [guess, sure, guess], layer=CODE)
    assert counts.edges == 1  # no evidence line counts as one line
    assert await _edges(db_session, snapshot) == {(CREATE, HELPER, "calls", 1.0, "scip")}
    # Another file finding the same edge less surely doesn't replace it.
    await writer.replace_file("app/util.py", [], [guess], layer=CODE)
    assert await _edges(db_session, snapshot) == {(CREATE, HELPER, "calls", 1.0, "scip")}
    # Another evidence line is another edge.
    elsewhere = GraphEdge(CREATE, HELPER, EdgeType.CALLS, Provenance.NAME_UNIQUE, evidence_line=5)
    await writer.replace_all([], [sure, elsewhere], layer=CODE)
    assert await _edges(db_session, snapshot) == {
        (CREATE, HELPER, "calls", 1.0, "scip"),
        (CREATE, HELPER, "calls", 0.4, "name_unique"),
    }


async def test_the_symbols_stage_run_again_leaves_the_graph_alone(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    writer = GraphWriter(db_session, snapshot)
    nodes, edges = _client_facts()
    await writer.replace_file("web/client.ts", nodes, edges, layer=LINKS)
    await writer.replace_file(
        "app/service.py",
        [],
        [GraphEdge(CREATE, HELPER, EdgeType.CALLS, Provenance.SCIP)],
        layer=CODE,
    )
    before = (await _nodes(db_session, snapshot), await _edges(db_session, snapshot))
    await _symbols(db_session, snapshot)  # a full re-run of the symbols stage
    after = (await _nodes(db_session, snapshot), await _edges(db_session, snapshot))
    assert after == before


async def test_the_writer_refuses_what_isnt_its_to_write(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    writer = GraphWriter(db_session, snapshot)
    with pytest.raises(GraphError, match="symbols stage"):
        await writer.replace_all(
            [GraphNode("py x.py/f().", NodeKind.FUNCTION, "f")], [], layer=CODE
        )
    with pytest.raises(GraphError, match="not a file"):
        await writer.replace_file("app/missing.py", [], [], layer=CODE)
    with pytest.raises(GraphError, match="not files"):
        await writer.replace_all(
            [GraphNode(ENDPOINT, NodeKind.API_ENDPOINT, "x", path="z.py")], [], layer=LINKS
        )
    with pytest.raises(GraphError, match="0 to 1"):
        await writer.replace_all(
            [],
            [GraphEdge(CREATE, HELPER, EdgeType.CALLS, Provenance.SCIP, confidence=1.5)],
            layer=CODE,
        )
    with pytest.raises(GraphError, match="not the code layer's: \\['issues_request edge'\\]"):
        await writer.replace_file("web/client.ts", [], _client_facts()[1], layer=CODE)
    with pytest.raises(GraphError, match="not the links layer's: \\['package node'\\]"):
        await writer.replace_all(
            [GraphNode("package py:os", NodeKind.PACKAGE, "os")], [], layer=LINKS
        )


async def test_the_graph_is_org_scoped(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    nodes, edges = _client_facts()
    await GraphWriter(db_session, snapshot).replace_file("web/client.ts", nodes, edges, layer=LINKS)
    assert await db_session.scalar(select(func.count()).select_from(CodeEdge)) == 1
    await set_org_context(db_session, uuid.uuid4())  # another org
    assert (await db_session.scalars(select(CodeEdge))).all() == []
    assert (
        await db_session.scalars(select(CodeNode).where(CodeNode.symbol_key == CALL))
    ).all() == []


async def test_the_graph_in_memory_is_read_within_its_org(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    nodes, edges = _client_facts()
    await GraphWriter(db_session, snapshot).replace_file("web/client.ts", nodes, edges, layer=LINKS)
    graph = await GraphSnapshot.build(db_session, snapshot.id)
    assert graph.node(CALL) is not None
    assert len(graph.edges) == 1
    await set_org_context(db_session, uuid.uuid4())  # another org: nothing of it
    elsewhere = await GraphSnapshot.build(db_session, snapshot.id)
    assert (len(elsewhere), len(elsewhere.edges)) == (0, 0)


async def test_a_graph_read_from_the_rows_answers_as_its_artifact_does(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Confidences are stored as REALs (0.9 reads back as 0.8999999761581421) and the artifact
    rounds them: read either way, ``min_conf=0.9`` keeps the same edges."""
    snapshot = await _snapshot(db_session, tmp_path)
    nodes, edges = _client_facts()
    sure = dataclasses.replace(edges[0], confidence=0.9)
    writer = GraphWriter(db_session, snapshot)
    await writer.replace_file("web/client.ts", nodes, [sure], layer=LINKS)
    rows = await GraphSnapshot.build(db_session, snapshot.id)
    artifact = GraphSnapshot.loads(rows.dumps())
    for graph in (rows, artifact):
        hops, _ = graph.neighbors(sure.src, min_conf=0.9)
        assert [hop.node.symbol_key for hop in hops] == [CALL]
        assert hops[0].edge.confidence == 0.9
        assert graph.shortest_path(sure.src, CALL, min_conf=0.9) is not None


async def test_definition_readers_never_see_the_graphs_nodes(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    nodes, edges = _client_facts()
    await GraphWriter(db_session, snapshot).replace_file("web/client.ts", nodes, edges, layer=LINKS)
    found = await find_symbols(db_session, snapshot.id, "items", limit=50)
    assert all(hit.symbol_key != CALL for hit in found)
    client_id = (await file_ids(db_session, snapshot.id))["web/client.ts"]
    at = await symbols_at(db_session, client_id, 2)
    assert [node.kind for node in at] == ["function", "module"]


async def test_a_modules_import_table_is_kept_on_its_node(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    module = "py app/service.py/"
    found = await GraphWriter(db_session, snapshot).annotate(
        {module: {"imports": {"helper": HELPER}}, "py nowhere.py/": {"imports": {}}}
    )
    assert found == 1  # only nodes that exist
    node = await db_session.scalar(select(CodeNode).where(CodeNode.symbol_key == module))
    assert node is not None
    await db_session.refresh(node)
    assert node.attrs["imports"] == {"helper": HELPER}
    assert node.kind == "module"  # merged into, not replaced


async def test_a_layer_written_again_leaves_the_others_facts(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session, tmp_path)
    writer = GraphWriter(db_session, snapshot)
    nodes, edges = _client_facts()
    await writer.replace_all(nodes, edges, layer=LINKS)
    os_package = GraphNode("package py:os", NodeKind.PACKAGE, "os")
    imports = GraphEdge("py app/service.py/", "package py:os", EdgeType.IMPORTS, Provenance.SCIP)
    calls = GraphEdge(CREATE, HELPER, EdgeType.CALLS, Provenance.SCIP)
    await writer.replace_all([os_package], [imports, calls], layer=CODE)
    links = ("ts web/client.ts/createItem().", CALL, "issues_request", 0.7, "receiver_heuristic")
    assert links in await _edges(db_session, snapshot)

    await writer.replace_all([], [calls], layer=CODE)  # the code again: its package gone
    assert await _edges(db_session, snapshot) == {(CREATE, HELPER, "calls", 1.0, "scip"), links}
    assert CALL in await _nodes(db_session, snapshot)
    assert "package py:os" not in await _nodes(db_session, snapshot)

    await writer.replace_file("web/client.ts", [], [], layer=CODE)  # nothing of links there
    assert await _edges(db_session, snapshot) == {(CREATE, HELPER, "calls", 1.0, "scip"), links}
    await writer.replace_all([], [], layer=LINKS)
    assert await _edges(db_session, snapshot) == {(CREATE, HELPER, "calls", 1.0, "scip")}
