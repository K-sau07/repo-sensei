"""Diagrams (6.1): a flow drawn from the graph, the model only choosing and labelling.

A diagram starts from a **subgraph** of the snapshot's graph: a trace's path (3.12), or, for a
question the trace can't follow, a bounded walk out from the symbols it names (depth at most
:data:`MAX_DEPTH`, :data:`FAN_OUT` children a node). Its nodes are numbered (``n1``...) and
shown to the fast model with the edges between them; the model answers with JSON naming those
ids (never code it hasn't been shown): which nodes to draw, with what labels, which edges, and,
for a step the graph can't show (a queue, dependency injection, reflection), an edge marked
inferred with the reason.

The **validator** checks the answer against the whole graph:
- a node must be one of the numbered ones;
- an edge must be in the graph, in its direction, and not one a model inferred
  (``llm_inferred``); otherwise it must say why it is inferred, and is drawn dashed.

An answer that fails is sent back with what was wrong, at most :data:`MAX_RETRIES` times; then
what is wrong is left out. ``verified_ratio`` is the share of the diagram's edges the graph
proves. Without a model (none configured, or it fails), the subgraph is drawn as it is: every
edge verified, its names as labels.

Each node carries its citation (file and lines, from the graph, not the model), and its
**members** as an entity diagram lists them: a class's or model's methods and fields, a table's
columns, a component's hooks and what it renders (:func:`with_members`, from the graph too).
Mermaid (flowchart or sequence) is compiled here, from the checked spec.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError

from sensei_core.logging import get_logger
from sensei_core.models import EdgeType, Provenance
from sensei_core.orgs.model_choices import StepChoice
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.graph.trace import TracePath, layer_of
from sensei_engine.llm.anthropic_client import (
    AnthropicClient,
    LLMError,
    LLMRequest,
    LLMUsageSink,
    Role,
    at_floor,
    tier_for,
)

log = get_logger(__name__)

PROMPT_VERSION = "diagram.v1"
DiagramKind = Literal["sequence", "flowchart", "architecture", "er"]
# What a model labels (an ER diagram is the schema's, from the graph: :mod:`sensei_engine.er`).
KINDS: tuple[DiagramKind, ...] = ("sequence", "flowchart", "architecture")
ALL_KINDS: tuple[DiagramKind, ...] = (*KINDS, "er")

MAX_DEPTH = 4  # a question's walk, in hops from what it names
FAN_OUT = 8  # children followed from each node
MAX_NODES = 40  # shown to the model
MAX_EDGES = 120
MAX_RETRIES = 2
MAX_TOKENS = 4_000
MAX_LABEL = 80
MAX_TITLE = 120
MAX_MEMBERS = 8  # listed on a node's card; the rest are counted
MEMBER_KINDS = frozenset({"method", "function", "variable", "class"})
# Edges a flow is drawn along (containment and definitions are structure, not flow).
FLOW_TYPES = frozenset(EdgeType) - {EdgeType.CONTAINS, EdgeType.DEFINES}


class DiagramError(ValueError):
    """Nothing to draw: the subgraph is empty, or no answer left an edge."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A node the model may draw, by its number."""

    id: str  # "n3"
    node: Node
    layer: str


@dataclass(frozen=True, slots=True)
class CandidateEdge:
    src: str  # candidate ids
    dst: str
    type: EdgeType
    confidence: float


@dataclass(slots=True)
class Subgraph:
    nodes: list[Candidate]
    edges: list[CandidateEdge]
    context: str  # what it is of: the question, or the trace's entry

    def by_id(self) -> dict[str, Candidate]:
        return {c.id: c for c in self.nodes}


# ---------------------------------------------------------------- subgraphs


def _numbered(
    keys: Iterable[str], graph: GraphSnapshot, layers: Mapping[str, str]
) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for key in keys:
        node = graph.node(key)
        if node is None or key in seen:
            continue
        seen.add(key)
        layer = layers.get(key) or layer_of(node, None)
        out.append(Candidate(f"n{len(out) + 1}", node, layer))
    return out


def _between(graph: GraphSnapshot, nodes: Sequence[Candidate]) -> list[CandidateEdge]:
    """The graph's flow edges among ``nodes`` (one per pair and type: the surest)."""
    ids = {c.node.symbol_key: c.id for c in nodes}
    best: dict[tuple[str, str, EdgeType], float] = {}
    for candidate in nodes:
        for there, edge in graph.edges_out(candidate.node.symbol_key):
            other = ids.get(there)
            if other is None or other == candidate.id or not _flows(edge):
                continue
            key = (candidate.id, other, edge.type)
            best[key] = max(best.get(key, 0.0), edge.confidence)
    edges = [CandidateEdge(a, b, t, c) for (a, b, t), c in best.items()]
    edges.sort(key=lambda e: (-e.confidence, _num(e.src), _num(e.dst), e.type.value))
    return edges[:MAX_EDGES]


def _flows(edge: Edge) -> bool:
    """A flow edge the graph knows (not containment, and not a model's guess)."""
    return edge.type in FLOW_TYPES and edge.provenance is not Provenance.LLM_INFERRED


def _num(candidate_id: str) -> int:
    return int(candidate_id[1:]) if candidate_id[1:].isdigit() else 0


def from_trace(graph: GraphSnapshot, path: TracePath, context: str) -> Subgraph:
    """A trace's path: its hops, in order, and the graph's edges among them."""
    hops = path.hops[:MAX_NODES]
    layers = {hop.node.symbol_key: hop.layer for hop in hops}
    nodes = _numbered((hop.node.symbol_key for hop in hops), graph, layers)
    if not nodes:
        raise DiagramError("the trace has no hops to draw")
    return Subgraph(nodes, _between(graph, nodes), context)


def from_entries(
    graph: GraphSnapshot,
    entries: Sequence[Node],
    context: str,
    *,
    depth: int = MAX_DEPTH,
    fan_out: int = FAN_OUT,
) -> Subgraph:
    """A walk out from ``entries`` along flow edges: at most ``depth`` hops (:data:`MAX_DEPTH`),
    the ``fan_out`` surest children of each node, :data:`MAX_NODES` in all, nearest first."""
    depth = max(1, min(depth, MAX_DEPTH))
    order: list[str] = []
    seen: set[str] = set()
    frontier = [n.symbol_key for n in entries if graph.node(n.symbol_key) is not None]
    for key in frontier:
        if key not in seen:
            seen.add(key)
            order.append(key)
    for _ in range(depth):
        following: list[str] = []
        for key in frontier:
            children = sorted(
                (
                    (edge.confidence, there)
                    for there, edge in graph.edges_out(key)
                    if _flows(edge) and there not in seen
                ),
                key=lambda item: (-item[0], item[1]),
            )
            for _, there in children[:fan_out]:
                if there in seen or len(order) >= MAX_NODES:
                    continue
                seen.add(there)
                order.append(there)
                following.append(there)
        frontier = following
        if not frontier or len(order) >= MAX_NODES:
            break
    nodes = _numbered(order, graph, {})
    if not nodes:
        raise DiagramError("nothing in the graph to start from")
    return Subgraph(nodes, _between(graph, nodes), context)


# ---------------------------------------------------------------- the spec


@dataclass(frozen=True, slots=True)
class Member:
    """Something a node holds or uses, as its card lists it."""

    name: str
    kind: str  # method, function, variable, class, column, hook, renders; a table's pk, fk
    type: str | None = None  # a column's
    ref: str | None = None  # the column a foreign key names ("user.id")
    nullable: bool | None = None

    def as_json(self) -> dict[str, Any]:
        found: dict[str, Any] = {"name": self.name, "kind": self.kind}
        for key in ("type", "ref", "nullable"):
            value = getattr(self, key)
            if value is not None:
                found[key] = value
        return found

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Member:
        nullable = raw.get("nullable")
        return cls(
            str(raw.get("name", "")), str(raw.get("kind", "")), _text(raw.get("type")),
            _text(raw.get("ref")), nullable if isinstance(nullable, bool) else None,
        )  # fmt: skip


@dataclass(frozen=True, slots=True)
class SpecNode:
    id: str  # the node's symbol key
    label: str
    layer: str
    kind: str
    path: str | None
    start_line: int
    end_line: int
    members: tuple[Member, ...] = ()
    more: int = 0  # members past MAX_MEMBERS, not listed
    # An architecture's part: what it does (a line), its icon, how much code it is.
    note: str | None = None
    icon: str | None = None
    size: int = 0

    def as_json(self) -> dict[str, Any]:
        extra: dict[str, Any] = {
            k: v for k, v in (("note", self.note), ("icon", self.icon)) if v is not None
        }
        if self.size:
            extra["size"] = self.size
        return extra | {
            "id": self.id,
            "label": self.label,
            "layer": self.layer,
            "kind": self.kind,
            "citation": (
                {"path": self.path, "start_line": self.start_line, "end_line": self.end_line}
                if self.path
                else None
            ),
            "members": [m.as_json() for m in self.members],
            "more_members": self.more,
        }


@dataclass(frozen=True, slots=True)
class Sample:
    """One of the code's own edges behind a line: what calls what, and where."""

    src: str  # the caller's name ("DonationController.create")
    dst: str
    path: str | None
    line: int | None

    def as_json(self) -> dict[str, Any]:
        return {"from": self.src, "to": self.dst, "path": self.path, "line": self.line}


@dataclass(frozen=True, slots=True)
class SpecEdge:
    src: str  # symbol keys
    dst: str
    type: str | None  # the graph's edge type (None: inferred)
    label: str | None
    verified: bool
    confidence: float | None
    provenance: str | None
    inferred: str | None  # why, when the graph doesn't show it
    # An architecture's line: how many of the code's edges it stands for, and some of them.
    count: int = 0
    samples: tuple[Sample, ...] = ()

    def as_json(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "from": self.src,
            "to": self.dst,
            "type": self.type,
            "label": self.label,
            "verified": self.verified,
            "confidence": self.confidence,
            "provenance": self.provenance,
            "inferred": {"reason": self.inferred} if self.inferred is not None else None,
        }
        if self.count:
            found["count"] = self.count
        if self.samples:
            found["samples"] = [sample.as_json() for sample in self.samples]
        return found


@dataclass(frozen=True, slots=True)
class Step:
    """A step of an architecture's walkthrough: one of its lines, said as what happens."""

    src: str
    dst: str
    text: str

    def as_json(self) -> dict[str, Any]:
        return {"from": self.src, "to": self.dst, "text": self.text}


@dataclass(slots=True)
class DiagramSpec:
    title: str
    kind: DiagramKind
    nodes: list[SpecNode]
    edges: list[SpecEdge] = field(default_factory=list[SpecEdge])
    summary: str | None = None  # what the system does, in a sentence (an architecture's)
    steps: list[Step] = field(default_factory=list[Step])  # how it works, line by line

    @property
    def verified_ratio(self) -> float:
        """The share of its edges the graph proves (1 with none to prove)."""
        if not self.edges:
            return 1.0
        return sum(1 for e in self.edges if e.verified) / len(self.edges)

    def as_json(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "title": self.title,
            "kind": self.kind,
            "nodes": [n.as_json() for n in self.nodes],
            "edges": [e.as_json() for e in self.edges],
        }
        if self.summary:
            found["summary"] = self.summary
        if self.steps:
            found["steps"] = [step.as_json() for step in self.steps]
        return found

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> DiagramSpec:
        """A stored spec back (as :meth:`as_json` wrote it)."""
        nodes: list[SpecNode] = []
        for raw in _items(data.get("nodes")):
            cited = _object(raw.get("citation"))
            members = tuple(Member.from_json(m) for m in _items(raw.get("members")))
            nodes.append(
                SpecNode(
                    str(raw.get("id", "")), str(raw.get("label", "")), str(raw.get("layer", "")),
                    str(raw.get("kind", "")), _text(cited.get("path")),
                    int(cited.get("start_line") or 0), int(cited.get("end_line") or 0),
                    members, int(raw.get("more_members") or 0), _text(raw.get("note")),
                    _text(raw.get("icon")), int(raw.get("size") or 0),
                )
            )  # fmt: skip
        edges: list[SpecEdge] = []
        for raw in _items(data.get("edges")):
            confidence = raw.get("confidence")
            edges.append(
                SpecEdge(
                    str(raw.get("from", "")), str(raw.get("to", "")), _text(raw.get("type")),
                    _text(raw.get("label")), bool(raw.get("verified")),
                    float(confidence) if isinstance(confidence, (int, float)) else None,
                    _text(raw.get("provenance")), _text(_object(raw.get("inferred")).get("reason")),
                    int(raw.get("count") or 0),
                    tuple(
                        Sample(
                            str(x.get("from", "")), str(x.get("to", "")), _text(x.get("path")),
                            int(x["line"]) if isinstance(x.get("line"), int) else None,
                        )
                        for x in _items(raw.get("samples"))
                    ),
                )
            )  # fmt: skip
        steps = [
            Step(str(x.get("from", "")), str(x.get("to", "")), str(x.get("text", "")))
            for x in _items(data.get("steps"))
        ]
        kind = str(data.get("kind", "flowchart"))
        shown: DiagramKind = "flowchart"
        for one in ALL_KINDS:
            if one == kind:
                shown = one
        return cls(
            str(data.get("title", "")), shown, nodes, edges, _text(data.get("summary")), steps
        )


def _items(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        cast("Mapping[str, Any]", v) for v in cast("list[object]", value) if isinstance(v, dict)
    ]


def _object(value: object) -> Mapping[str, Any]:
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------- the model's answer


class DraftNode(BaseModel):
    id: str
    label: str


class DraftEdge(BaseModel):
    source: str
    target: str
    label: str
    inferred_reason: str


class Draft(BaseModel):
    title: str
    nodes: list[DraftNode]
    edges: list[DraftEdge]


def answer_schema() -> dict[str, Any]:
    node = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
        "required": ["id", "label"],
        "additionalProperties": False,
    }
    edge = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "target": {"type": "string"},
            "label": {"type": "string"},
            "inferred_reason": {"type": "string"},  # "" when the edge is one of the graph's
        },
        "required": ["source", "target", "label", "inferred_reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "nodes": {"type": "array", "items": node},
            "edges": {"type": "array", "items": edge},
        },
        "required": ["title", "nodes", "edges"],
        "additionalProperties": False,
    }


def system_prompt() -> str:
    path = resources.files("sensei_engine.llm").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


def render(sub: Subgraph, kind: DiagramKind) -> str:
    """What the model is shown: what the diagram is of, its kind, the numbered nodes and the
    graph's edges among them."""
    lines = [f"Diagram of: {sub.context}", f"Kind: {kind}", "", "Nodes:"]
    for c in sub.nodes:
        where = f" {c.node.path}:{c.node.start_line}-{c.node.end_line}" if c.node.path else ""
        lines.append(
            f"{c.id} [{c.layer}] {c.node.kind} {c.node.qualified_name or c.node.name}{where}"
        )
    lines += ["", "Edges (the graph's):"]
    lines += [f"{e.src} -> {e.dst} {e.type.value}" for e in sub.edges] or ["(none)"]
    return "\n".join(lines)


def _clean(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(slots=True)
class Checked:
    spec: DiagramSpec
    problems: list[str]


def validate(draft: Draft, sub: Subgraph, graph: GraphSnapshot, kind: DiagramKind) -> Checked:
    """The draft against the graph (see the module): the spec of what holds, and what doesn't."""
    candidates = sub.by_id()
    problems: list[str] = []
    nodes: list[SpecNode] = []
    drawn: dict[str, Candidate] = {}
    for item in draft.nodes:
        handle = item.id.strip().lower()
        found = candidates.get(handle)
        if found is None:
            problems.append(f"node {item.id!r} isn't one of the numbered nodes")
            continue
        if handle in drawn:
            continue
        drawn[handle] = found
        node = found.node
        nodes.append(
            SpecNode(
                node.symbol_key,
                _clean(item.label, MAX_LABEL) or node.name,
                found.layer,
                node.kind,
                node.path,
                node.start_line,
                node.end_line,
            )
        )
    edges: list[SpecEdge] = []
    seen: set[tuple[str, str]] = set()
    for item in draft.edges:
        a, b = item.source.strip().lower(), item.target.strip().lower()
        if a not in drawn or b not in drawn:
            missing = item.source if a not in drawn else item.target
            problems.append(f"edge {item.source} -> {item.target}: {missing} isn't a drawn node")
            continue
        if a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        src, dst = drawn[a].node.symbol_key, drawn[b].node.symbol_key
        proof = _proof(graph, src, dst)
        label = _clean(item.label, MAX_LABEL) or None
        reason = _clean(item.inferred_reason, MAX_LABEL * 2)
        if proof is not None:
            edges.append(
                SpecEdge(
                    src, dst, proof.type.value, label, True, proof.confidence,
                    proof.provenance.value, None,
                )
            )  # fmt: skip
        elif reason:
            edges.append(SpecEdge(src, dst, None, label, False, None, None, reason))
        elif _proof(graph, dst, src) is not None:
            problems.append(
                f"edge {item.source} -> {item.target} is the other way in the graph "
                f"({item.target} -> {item.source}); draw it that way, or say why it's inferred"
            )
        else:
            problems.append(
                f"edge {item.source} -> {item.target} isn't in the graph; leave it out, or give "
                "inferred_reason"
            )
    title = _clean(draft.title, MAX_TITLE) or sub.context[:MAX_TITLE]
    if not edges and len(nodes) > 1:
        problems.append("the diagram has no edges: draw the flow between the nodes")
    return Checked(DiagramSpec(title, kind, nodes, edges), problems)


def _proof(graph: GraphSnapshot, src: str, dst: str) -> Edge | None:
    """The surest edge of the graph from ``src`` to ``dst`` that a model didn't infer."""
    best: Edge | None = None
    for there, edge in graph.edges_out(src):
        if there != dst or not _flows(edge):
            continue
        if best is None or edge.confidence > best.confidence:
            best = edge
    return best


def plain(sub: Subgraph, kind: DiagramKind, graph: GraphSnapshot) -> DiagramSpec:
    """The subgraph drawn as it is (no model): its names as labels, every edge the graph's."""
    draft = Draft(
        title=sub.context,
        nodes=[DraftNode(id=c.id, label=c.node.name) for c in sub.nodes],
        edges=[
            DraftEdge(source=e.src, target=e.dst, label="", inferred_reason="") for e in sub.edges
        ],
    )
    return validate(draft, sub, graph, kind).spec


@dataclass(slots=True)
class Generated:
    spec: DiagramSpec
    tries: int  # the model's answers read (0: drawn without one)
    problems: list[str]  # what the last answer had wrong (left out of the spec)


def with_members(spec: DiagramSpec, graph: GraphSnapshot) -> DiagramSpec:
    """``spec`` with each node's members, from the graph (one pass over its nodes):
    - a class, a model or a component: the definitions inside it (``Item.save``, one level
      down, in its file);
    - a table: its columns;
    - a component: also the hooks it uses and the components it renders."""
    found: dict[str, list[Member]] = {n.id: [] for n in spec.nodes}
    inside, tables = _owners(spec, graph)
    if inside or tables:
        _held(graph, inside, tables, found)
    _used(spec, graph, found)
    nodes: list[SpecNode] = []
    for spec_node in spec.nodes:
        members = list(dict.fromkeys(found.get(spec_node.id, [])))  # in order, once
        nodes.append(
            SpecNode(
                spec_node.id, spec_node.label, spec_node.layer, spec_node.kind, spec_node.path,
                spec_node.start_line, spec_node.end_line, tuple(members[:MAX_MEMBERS]),
                max(0, len(members) - MAX_MEMBERS),
            )
        )  # fmt: skip
    return DiagramSpec(spec.title, spec.kind, nodes, spec.edges)


def _owners(
    spec: DiagramSpec, graph: GraphSnapshot
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """What can hold members: (file, "Qualified.") of a class, model or component, and
    "table." of a table, each to its node's id."""
    inside: dict[tuple[str, str], str] = {}
    tables: dict[str, str] = {}
    for spec_node in spec.nodes:
        node = graph.node(spec_node.id)
        if node is None:
            continue
        if node.kind == "table":
            tables[f"{node.name.lower()}."] = spec_node.id
        elif node.path and node.kind in {"class", "orm_model", "component"}:
            inside[(node.path, f"{node.qualified_name or node.name}.")] = spec_node.id
    return inside, tables


def _held(
    graph: GraphSnapshot,
    inside: Mapping[tuple[str, str], str],
    tables: Mapping[str, str],
    found: dict[str, list[Member]],
) -> None:
    """Each owner's definitions one level down, and each table's columns."""
    paths = {path for path, _ in inside}
    for node in graph.nodes:
        qualified = node.qualified_name or node.name
        if node.kind == "column":
            table = tables.get(qualified.lower().split(".", 1)[0] + ".")
            if table is not None:
                found[table].append(Member(node.name, "column"))
            continue
        if node.path not in paths or node.kind not in MEMBER_KINDS or "." not in qualified:
            continue
        head, _, name = qualified.rpartition(".")
        owner = inside.get((node.path, f"{head}."))
        if owner is not None and name:
            found[owner].append(Member(name, node.kind))


def _used(spec: DiagramSpec, graph: GraphSnapshot, found: dict[str, list[Member]]) -> None:
    """A component's hooks and the components it renders."""
    for spec_node in spec.nodes:
        if spec_node.kind != "component":
            continue
        for there, edge in graph.edges_out(spec_node.id):
            if edge.type not in (EdgeType.USES_HOOK, EdgeType.RENDERS):
                continue
            target = graph.node(there)
            if target is not None:
                kind = "hook" if edge.type is EdgeType.USES_HOOK else "renders"
                found[spec_node.id].append(Member(target.name, kind))


async def generate(
    client: AnthropicClient | None,
    sub: Subgraph,
    graph: GraphSnapshot,
    kind: DiagramKind,
    *,
    on_usage: LLMUsageSink | None = None,
    max_retries: int = MAX_RETRIES,
    floor: StepChoice | None = None,
) -> Generated:
    """The diagram of ``sub`` (see the module), each node with its members. Raises
    :class:`DiagramError` when nothing is left to draw. ``floor``: the org's for diagrams
    (10.7.2), raising the labels' model or effort."""
    made = await _generate(
        client, sub, graph, kind, on_usage=on_usage, max_retries=max_retries, floor=floor
    )
    return Generated(with_members(made.spec, graph), made.tries, made.problems)


async def _generate(
    client: AnthropicClient | None,
    sub: Subgraph,
    graph: GraphSnapshot,
    kind: DiagramKind,
    *,
    on_usage: LLMUsageSink | None = None,
    max_retries: int = MAX_RETRIES,
    floor: StepChoice | None = None,
) -> Generated:
    if not sub.nodes:
        raise DiagramError("nothing to draw")
    if client is None or not client.configured:
        return Generated(plain(sub, kind, graph), 0, [])
    tier, effort = tier_for(Role.ROUTER), "low"
    if floor is not None:
        tier, effort = at_floor(floor, tier, effort)
    base = render(sub, kind)
    feedback: list[str] = []
    checked: Checked | None = None
    tries = 0
    for _ in range(1 + max_retries):
        prompt = base
        if feedback:
            prompt += "\n\nYour last answer had these problems; answer again:\n" + "\n".join(
                f"- {p}" for p in feedback
            )
        request = LLMRequest(
            prompt,
            system=system_prompt(),
            tier=tier,
            max_tokens=MAX_TOKENS,
            effort=effort,
            schema=answer_schema(),
            cache_system=True,
        )
        try:
            result = await client.complete(request, on_usage=on_usage)
            draft = Draft.model_validate(result.data)
        except (LLMError, ValidationError) as exc:
            log.warning("diagram answer unusable", error=str(exc))
            break
        tries += 1
        checked = validate(draft, sub, graph, kind)
        if not checked.problems:
            return Generated(checked.spec, tries, [])
        feedback = checked.problems
    if checked is None or (not checked.spec.edges and len(checked.spec.nodes) != 1):
        if checked is not None:
            log.info("diagram drawn from the graph", problems=len(checked.problems))
        return Generated(plain(sub, kind, graph), tries, checked.problems if checked else [])
    return Generated(checked.spec, tries, checked.problems)


# ---------------------------------------------------------------- mermaid


_UNSAFE = re.compile(r'["`<>{}\[\]|#;]')


def _label(text: str) -> str:
    return _UNSAFE.sub(" ", text).strip() or "?"


def mermaid(spec: DiagramSpec, *, link: Callable[[SpecNode], str | None] | None = None) -> str:
    """The spec as Mermaid: a sequence diagram, an ER diagram (a schema's), or a flowchart (an
    architecture one grouped by layer); inferred edges dashed, each node linked to its code when
    ``link`` gives a URL."""
    ids = {node.id: f"n{i + 1}" for i, node in enumerate(spec.nodes)}
    urls = {node.id: url for node in spec.nodes if link and (url := link(node))}
    if spec.kind == "sequence":
        lines = _sequence(spec, ids, urls)
    elif spec.kind == "er":
        lines = _er(spec, ids)
    else:
        lines = _flowchart(spec, ids, urls)
    return "\n".join(lines) + "\n"


_ER_NAME = re.compile(r"[^A-Za-z0-9_]")
# A relation's ends in Mermaid's crow's feet: many (or one) of the table holding the key to one
# of the table it names, which may be none when the key may be null.
_ER_ENDS = {
    ("many_to_one", False): "}o--||", ("many_to_one", True): "}o--o|",
    ("one_to_one", False): "|o--||", ("one_to_one", True): "|o--o|",
}  # fmt: skip


def _er(spec: DiagramSpec, ids: Mapping[str, str]) -> list[str]:
    """``erDiagram``: each table with its columns (type, PK, FK), each foreign key a relation."""
    names: dict[str, str] = {}
    for node in spec.nodes:
        name = _ER_NAME.sub("_", node.label) or ids[node.id]
        names[node.id] = name if name not in names.values() else f"{name}_{ids[node.id]}"
    lines = ["erDiagram"]
    for node in spec.nodes:
        lines.append(f"  {names[node.id]} {{")
        for member in node.members:
            kind = _ER_NAME.sub("_", member.type or "") or "column"
            keys = ",".join(
                k for k, on in (("PK", member.kind == "pk"), ("FK", bool(member.ref))) if on
            )
            lines.append(
                f"    {kind} {_ER_NAME.sub('_', member.name) or '_'}{' ' + keys if keys else ''}"
            )
        lines.append("  }")
    nullable = {
        (node.id, m.name): bool(m.nullable) for node in spec.nodes for m in node.members if m.ref
    }
    for edge in spec.edges:
        if edge.src not in names or edge.dst not in names:
            continue
        column = (edge.label or "").split(" ", 1)[0]
        ends = _ER_ENDS.get((edge.type or "", nullable.get((edge.src, column), False)), "}o--||")
        lines.append(f'  {names[edge.src]} {ends} {names[edge.dst]} : "{_label(edge.label or "")}"')
    return lines


def _href(url: str) -> str:
    return url.replace('"', "%22")


def _sequence(spec: DiagramSpec, ids: Mapping[str, str], urls: Mapping[str, str]) -> list[str]:
    lines = ["sequenceDiagram"]
    lines += [f"  participant {ids[n.id]} as {_label(n.label)}" for n in spec.nodes]
    for edge in spec.edges:
        arrow = "->>" if edge.verified else "-->>"
        text = _label(edge.label or edge.type or edge.inferred or "")
        lines.append(f"  {ids[edge.src]}{arrow}{ids[edge.dst]}: {text}")
    lines += [f"  link {ids[key]}: code @ {_href(url)}" for key, url in urls.items()]
    return lines


def _flowchart(spec: DiagramSpec, ids: Mapping[str, str], urls: Mapping[str, str]) -> list[str]:
    lines = ["flowchart LR"]
    if spec.kind == "architecture":
        layers: dict[str, list[SpecNode]] = {}
        for node in spec.nodes:
            layers.setdefault(node.layer, []).append(node)
        for layer, members in layers.items():
            lines.append(f"  subgraph {_label(layer)}")
            lines += [f'    {ids[n.id]}["{_label(n.label)}"]' for n in members]
            lines.append("  end")
    else:
        lines += [f'  {ids[n.id]}["{_label(n.label)}"]' for n in spec.nodes]
    for edge in spec.edges:
        arrow = "-->" if edge.verified else "-.->"
        text = edge.label or (edge.inferred if not edge.verified else None)
        middle = f"|{_label(text)}|" if text else ""
        lines.append(f"  {ids[edge.src]} {arrow}{middle} {ids[edge.dst]}")
    dashed = [str(i) for i, edge in enumerate(spec.edges) if not edge.verified]
    if dashed:
        lines.append(f"  linkStyle {','.join(dashed)} stroke-dasharray:4 3")
    lines += [f'  click {ids[key]} href "{_href(url)}"' for key, url in urls.items()]
    return lines
