"""Writing a snapshot's graph facts (3.1): the nodes the graph stages find and their edges.

Facts are named, not numbered: a node by its key (unique in the snapshot, as a definition's
symbol key is), an edge by the keys of its ends; the writer resolves them to rows. Definitions
(:data:`~sensei_core.models.SYMBOL_KINDS`) are the symbols stage's: the writer adds edges between
them and writes nodes of the graph's own kinds (an endpoint, an ORM model, a package, ...), never
a definition.

**Ownership.** A fact is owned by the file whose extraction produced it (``owner``: its path), or
by no file (a snapshot-wide fact, such as an HTTP call matched to an endpoint). When a file
changes, :meth:`GraphWriter.replace_file` deletes everything the file owns and writes what it
holds now, in the caller's transaction: no fact outlives its evidence, and the nodes it writes
again keep their ids (an upsert on the key), so edges other files own into them survive. A node
that goes takes its edges with it. A full index writes the whole snapshot at once
(:meth:`GraphWriter.replace_all`).

**Duplicates.** One edge per (ends, type, evidence line): written twice, the more certain one
stays. An edge whose end isn't a node of the snapshot (a symbol not extracted, a typo in a key)
is left out and counted (``unresolved``).

Rows go in with ``COPY`` into temporary tables, then ``INSERT … SELECT`` joined to the snapshot's
files and nodes (row-level security checks every row).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import SYMBOL_KINDS, EdgeType, NodeKind, Provenance, Snapshot
from sensei_engine.chunk.store import SnapshotKey
from sensei_engine.graph.confidence import confidence_of

NODE_STAGING = "graph_nodes_staging"
EDGE_STAGING = "graph_edges_staging"


class GraphError(ValueError):
    """Facts the writer won't write (a definition's kind, a path that isn't the snapshot's)."""


@dataclass(frozen=True, slots=True)
class GraphNode:
    key: str  # unique in the snapshot: '<kind> <what>' ("endpoint GET /api/items/{}")
    kind: NodeKind
    name: str
    path: str | None = None  # the file it is in (None: in none)
    start_line: int = 0  # 1-based, 0: no lines
    end_line: int = 0
    qualified_name: str | None = None  # the name when None
    lang: str = ""
    attrs: Mapping[str, Any] = field(default_factory=dict[str, Any])
    owner: str | None = None  # the path of the file that produced it (None: no file)


@dataclass(frozen=True, slots=True)
class GraphEdge:
    src: str  # node keys (a definition's symbol key, or a GraphNode's key)
    dst: str
    type: EdgeType
    provenance: Provenance
    confidence: float | None = None  # None: the provenance's (confidence_of)
    evidence_path: str | None = None  # the file and line that prove it
    evidence_line: int | None = None
    attrs: Mapping[str, Any] = field(default_factory=dict[str, Any])
    owner: str | None = None  # the path of the file that produced it (None: no file)


@dataclass(slots=True)
class GraphCounts:
    nodes: int = 0  # written (inserted, or changed)
    edges: int = 0  # written
    unresolved: int = 0  # edges left out: an end isn't a node of the snapshot


def _text(value: str) -> str:
    return value.replace("\x00", "�")  # Postgres text can't hold NUL


def _json(value: Mapping[str, Any]) -> str:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode().replace("\\u0000", "\\ufffd")


def _hash(node: GraphNode) -> bytes:
    facts = [node.key, node.kind.value, node.name, node.path, node.start_line, node.end_line]
    return hashlib.sha256(orjson.dumps([*facts, _json(node.attrs)])).digest()


class GraphWriter:
    """Writes one snapshot's graph facts (see the module), in the caller's transaction (which
    must have the snapshot's org set)."""

    def __init__(self, db: AsyncSession, snapshot: Snapshot | SnapshotKey) -> None:
        self._db = db
        self._ids = (snapshot.org_id, snapshot.repo_id, snapshot.id)

    async def replace_file(
        self, path: str, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]
    ) -> GraphCounts:
        """Everything the file at ``path`` owns, replaced by ``nodes`` and ``edges`` (owned by it
        whatever their ``owner`` says)."""
        driver = await self._driver()
        snapshot_id = self._ids[2]
        owner = await driver.fetchval(
            "SELECT id FROM public.files WHERE snapshot_id = $1 AND path = $2", snapshot_id, path
        )
        if owner is None:
            msg = f"not a file of the snapshot: {path!r}"
            raise GraphError(msg)
        owned_nodes = [replace(node, owner=path) for node in nodes]
        owned_edges = [replace(edge, owner=path) for edge in edges]
        await driver.execute(
            "DELETE FROM public.edges WHERE snapshot_id = $1 AND owner_file_id = $2",
            snapshot_id,
            owner,
        )
        await driver.execute(
            "DELETE FROM public.nodes WHERE snapshot_id = $1 AND owner_file_id = $2 "
            "AND kind <> ALL($3) AND NOT (symbol_key = ANY($4))",
            snapshot_id,
            owner,
            list(SYMBOL_KINDS),
            [node.key for node in owned_nodes],
        )
        counts = GraphCounts(nodes=await self._write_nodes(driver, owned_nodes))
        counts.edges, counts.unresolved = await self._write_edges(driver, owned_edges)
        return counts

    async def replace_all(
        self, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]
    ) -> GraphCounts:
        """The snapshot's whole graph (a full index): every edge, and every graph node not among
        ``nodes``, replaced. Each fact is owned by its ``owner``."""
        driver = await self._driver()
        snapshot_id = self._ids[2]
        listed = list(nodes)
        await driver.execute("DELETE FROM public.edges WHERE snapshot_id = $1", snapshot_id)
        await driver.execute(
            "DELETE FROM public.nodes WHERE snapshot_id = $1 AND kind <> ALL($2) "
            "AND NOT (symbol_key = ANY($3))",
            snapshot_id,
            list(SYMBOL_KINDS),
            [node.key for node in listed],
        )
        counts = GraphCounts(nodes=await self._write_nodes(driver, listed))
        counts.edges, counts.unresolved = await self._write_edges(driver, edges)
        return counts

    async def _driver(self) -> Any:
        connection = await self._db.connection()
        raw = await connection.get_raw_connection()
        return cast("Any", raw.driver_connection)  # asyncpg.Connection

    async def _write_nodes(self, driver: Any, nodes: Sequence[GraphNode]) -> int:
        definitions = sorted({node.key for node in nodes if node.kind.value in SYMBOL_KINDS})
        if definitions:
            msg = f"definitions are the symbols stage's, not the graph's: {definitions[:3]}"
            raise GraphError(msg)
        if not nodes:
            return 0
        org_id, repo_id, snapshot_id = self._ids
        await driver.execute(
            f"CREATE TEMPORARY TABLE {NODE_STAGING} (key text, kind text, name text, "
            "qualified_name text, path text, start_line int, end_line int, lang text, "
            "attrs text, content_hash bytea, owner text) ON COMMIT DROP"
        )
        await driver.copy_records_to_table(
            NODE_STAGING,
            records=[
                (
                    _text(node.key),
                    node.kind.value,
                    _text(node.name),
                    _text(node.qualified_name or node.name),
                    node.path,
                    node.start_line,
                    node.end_line,
                    node.lang,
                    _json(node.attrs),
                    _hash(node),
                    node.owner,
                )
                for node in nodes
            ],
        )
        try:
            missing = await driver.fetch(
                f"SELECT DISTINCT p FROM {NODE_STAGING} s, unnest(ARRAY[s.path, s.owner]) p "  # noqa: S608 - constant names
                "WHERE p IS NOT NULL AND NOT EXISTS (SELECT FROM public.files f "
                "WHERE f.snapshot_id = $1 AND f.path = p) LIMIT 3",
                snapshot_id,
            )
            if missing:
                msg = f"not files of the snapshot: {[row['p'] for row in missing]}"
                raise GraphError(msg)
            status = await driver.execute(
                "INSERT INTO public.nodes AS n (org_id, repo_id, snapshot_id, kind, symbol_key, "  # noqa: S608 - constant names
                "name, qualified_name, file_id, start_line, end_line, start_col, end_col, lang, "
                "attrs, content_hash, owner_file_id) "
                "SELECT DISTINCT ON (s.key) $1, $2, $3, s.kind, s.key, s.name, s.qualified_name, "
                "f.id, s.start_line, s.end_line, 0, 0, s.lang, s.attrs::jsonb, s.content_hash, "
                f"o.id FROM {NODE_STAGING} s "
                "LEFT JOIN public.files f ON f.snapshot_id = $3 AND f.path = s.path "
                "LEFT JOIN public.files o ON o.snapshot_id = $3 AND o.path = s.owner "
                "ORDER BY s.key "
                "ON CONFLICT (snapshot_id, symbol_key) DO UPDATE SET kind = EXCLUDED.kind, "
                "name = EXCLUDED.name, qualified_name = EXCLUDED.qualified_name, "
                "file_id = EXCLUDED.file_id, start_line = EXCLUDED.start_line, "
                "end_line = EXCLUDED.end_line, lang = EXCLUDED.lang, attrs = EXCLUDED.attrs, "
                "content_hash = EXCLUDED.content_hash, owner_file_id = EXCLUDED.owner_file_id "
                # Never a definition; a node unchanged isn't written again.
                "WHERE n.kind <> ALL($4) AND (n.content_hash, n.owner_file_id) "
                "IS DISTINCT FROM (EXCLUDED.content_hash, EXCLUDED.owner_file_id)",
                org_id,
                repo_id,
                snapshot_id,
                list(SYMBOL_KINDS),
            )
        finally:
            await driver.execute(f"DROP TABLE IF EXISTS {NODE_STAGING}")
        return int(status.rsplit(" ", 1)[-1])

    async def _write_edges(self, driver: Any, edges: Iterable[GraphEdge]) -> tuple[int, int]:
        records = [
            (
                _text(edge.src),
                _text(edge.dst),
                edge.type.value,
                edge.confidence if edge.confidence is not None else confidence_of(edge.provenance),
                edge.provenance.value,
                edge.evidence_path,
                edge.evidence_line,
                _json(edge.attrs),
                edge.owner,
            )
            for edge in edges
        ]
        if not records:
            return 0, 0
        for record in records:
            if not 0 <= record[3] <= 1:
                msg = f"a confidence is 0 to 1, not {record[3]} ({record[0]} -> {record[1]})"
                raise GraphError(msg)
        org_id, repo_id, snapshot_id = self._ids
        await driver.execute(
            f"CREATE TEMPORARY TABLE {EDGE_STAGING} (src text, dst text, type text, "
            "confidence real, provenance text, evidence_path text, evidence_line int, "
            "attrs text, owner text) ON COMMIT DROP"
        )
        await driver.copy_records_to_table(EDGE_STAGING, records=records)
        try:
            resolved = (
                f"FROM {EDGE_STAGING} s "
                "JOIN public.nodes a ON a.snapshot_id = $3 AND a.symbol_key = s.src "
                "JOIN public.nodes b ON b.snapshot_id = $3 AND b.symbol_key = s.dst "
            )
            status = await driver.execute(
                "INSERT INTO public.edges AS e (org_id, repo_id, snapshot_id, src_id, dst_id, "
                "type, confidence, provenance, evidence_file_id, evidence_line, owner_file_id, "
                "attrs) "
                "SELECT DISTINCT ON (a.id, b.id, s.type, s.evidence_line) $1, $2, $3, a.id, b.id, "
                "s.type::edge_type, s.confidence, s.provenance::edge_provenance, ef.id, "
                f"s.evidence_line, o.id, s.attrs::jsonb {resolved}"
                "LEFT JOIN public.files ef ON ef.snapshot_id = $3 AND ef.path = s.evidence_path "
                "LEFT JOIN public.files o ON o.snapshot_id = $3 AND o.path = s.owner "
                "ORDER BY a.id, b.id, s.type, s.evidence_line, s.confidence DESC "
                "ON CONFLICT (snapshot_id, src_id, dst_id, type, evidence_line) DO UPDATE SET "
                "confidence = EXCLUDED.confidence, provenance = EXCLUDED.provenance, "
                "evidence_file_id = EXCLUDED.evidence_file_id, attrs = EXCLUDED.attrs, "
                "owner_file_id = EXCLUDED.owner_file_id "
                "WHERE EXCLUDED.confidence > e.confidence",  # the more certain stays
                org_id,
                repo_id,
                snapshot_id,
            )
            unresolved = await driver.fetchval(
                f"SELECT count(*) FROM {EDGE_STAGING} s WHERE NOT EXISTS ("  # noqa: S608 - constant names
                "SELECT FROM public.nodes a WHERE a.snapshot_id = $1 AND a.symbol_key = s.src) "
                "OR NOT EXISTS (SELECT FROM public.nodes b WHERE b.snapshot_id = $1 "
                "AND b.symbol_key = s.dst)",
                snapshot_id,
            )
        finally:
            await driver.execute(f"DROP TABLE IF EXISTS {EDGE_STAGING}")
        return int(status.rsplit(" ", 1)[-1]), int(unresolved or 0)
