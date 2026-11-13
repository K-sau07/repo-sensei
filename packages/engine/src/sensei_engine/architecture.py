"""The whole app's architecture (6.6): how a repository is built, as a system diagram. Its
boxes are named and grouped by a model; every line between them is the graph's.

:func:`facts_of` reads the graph (no model):

- its **modules** (the dependency view's, :mod:`sensei_engine.deps`): the largest
  :data:`MAX_PARTS` of them with code in a layer (tests, configuration and the like only when
  there is little else), each in the tier most of its code is in (frontend, API, services,
  data), each with what it holds: its endpoints, tables and models, pages and components, hooks
  and services, up to :data:`MAX_MEMBERS`;
- the **lines** between them: the graph's own edges between their code, counted;
- the **outside systems** its code uses, each known by the packages that reach it
  (:data:`KNOWN`: ``psycopg`` is PostgreSQL, ``redis`` Redis, ``stripe`` Stripe), and which
  modules import them; and the modules whose code reads and writes the tables, the schema's
  database (the one database it imports a driver for, else a database by that name).

:func:`draw` asks the model to group the modules into the parts of the system a person would
draw on a whiteboard ("Web app", "Items API", "Auth service"), each with its role, an icon and
a line on what it does; it never draws a line. The answer is checked: every module once, in
one part, by its id; what it gets wrong is sent back once, then the plain grouping
(:func:`plain`: a part per module, named by its folder) stands for what is left. The lines
between parts are the modules' lines (each verified: the graph's), a part's line to an outside
system its imports (verified), and the people using it, drawn above, reach its frontend (or
its API, with none): that one line is inferred, and says why. Each line keeps the code's own
edges behind it (:class:`~sensei_engine.diagrams.Sample`: caller, callee, where), and each part
its folders, what they hold and their code most joined to other modules'.

A second call (:func:`explain`) says what each line does, from the code behind it ("places
donations"), and walks through how the app works, a step per line; a label or step for a line
the diagram doesn't have is left out. Tooling, scripts and docs (role ``other``) aren't drawn.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from sensei_core.logging import get_logger
from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_core.orgs.model_choices import StepChoice
from sensei_engine.deps import DEPENDS, MIN_CONF, Grouping, group_of
from sensei_engine.diagrams import (
    MAX_MEMBERS,
    DiagramSpec,
    Member,
    Sample,
    SpecEdge,
    SpecNode,
    Step,
)
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.graph.trace import layer_of
from sensei_engine.llm.anthropic_client import (
    AnthropicClient,
    LLMError,
    LLMRequest,
    LLMUsageSink,
    Tier,
    at_floor,
)
from sensei_engine.third_party import usage

log = get_logger(__name__)

PROMPT_VERSION = "architecture.v1"
FLOW_PROMPT_VERSION = "architecture-flow.v1"
MAX_PARTS = 24  # modules shown to the model
MAX_PLAIN = 12  # parts drawn without one
MAX_LINES = 60
MAX_OUTSIDE = 10
MIN_PARTS_IN_LAYERS = 4  # fewer modules with code in a layer than this: the rest are shown too
MAX_COMPONENTS = 12
MAX_NAME = 40
MAX_NOTE = 100
MAX_SUMMARY = 240
MAX_TOKENS = 4_000
MAX_RETRIES = 1
MAX_SAMPLES = 6  # of the code's edges kept on a line
MAX_KEY = 8  # a module's most connected code, listed
MAX_PART_MEMBERS = 24  # a part's folders and what they hold
MAX_PHRASE = 40
MAX_STEP = 200
MAX_STEPS = 10
KEY_KINDS = frozenset({"class", "function", "method", "component", "hook"})
Role = Literal["frontend", "api", "service", "worker", "data", "other"]
ROLES: tuple[Role, ...] = ("frontend", "api", "service", "worker", "data", "other")
ICONS: tuple[str, ...] = (
    "monitor", "layout", "smartphone", "globe", "server", "plug", "shield", "lock", "key",
    "users", "user", "cog", "workflow", "credit-card", "shopping-cart", "package", "mail", "bell",
    "message", "search", "file", "image", "chart", "clock", "bot", "brain", "database", "box",
    "wrench", "terminal",
)  # fmt: skip
# A module's tier, by its layer.
TIER_OF: Mapping[str, Role] = {
    "ui": "frontend", "state": "frontend", "client": "frontend", "route": "api",
    "handler": "api", "service": "service", "db": "data", "other": "other",
}  # fmt: skip
ROLE_ICON: Mapping[str, str] = {
    "frontend": "monitor", "api": "server", "service": "cog", "worker": "workflow",
    "data": "database", "other": "box",
}  # fmt: skip
# What a part's card lists first: what it serves, what it stores, what it shows, what it uses.
MEMBER_ORDER: tuple[tuple[str, str], ...] = (
    ("api_endpoint", "endpoint"),
    ("graphql_operation", "endpoint"),
    ("rpc_method", "endpoint"),
    ("table", "table"),
    ("orm_model", "model"),
    ("component", "component"),
    ("hook", "hook"),
    ("service", "service"),
    ("topic", "topic"),
)
RANK: dict[str, int] = {kind: i for i, (kind, _) in enumerate(MEMBER_ORDER)}
SAID: dict[str, str] = dict(MEMBER_ORDER)
# How a line is said by its most common edge type ("14 calls").
VERBS = {
    "calls": "calls", "imports": "imports", "references": "references", "renders": "renders",
    "uses_hook": "uses", "injects": "injects", "issues_request": "requests",
    "targets": "HTTP requests", "handled_by": "handles", "reads": "reads", "writes": "writes",
    "maps_to": "maps", "inherits": "extends", "implements": "implements",
    "publishes": "publishes", "consumes": "consumes", "tests": "tests",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Known:
    """An outside system, as a package reaching it says."""

    name: str
    kind: str  # database, cache, search, storage, queue, email, payments, ai, monitoring, auth,
    # messaging, cloud


_DB, _CACHE, _QUEUE, _AI = "database", "cache", "queue", "ai"


def _each(ecosystem: str, names: str, known: Known) -> dict[tuple[str, str], Known]:
    return {(ecosystem, name): known for name in names.split()}


# By (ecosystem, import name): a Python module (its dotted prefixes are tried, longest first)
# or an npm package (or its scope, "@sentry").
KNOWN: Mapping[tuple[str, str], Known] = {
    **_each("pypi", "psycopg psycopg2 asyncpg pg8000", Known("PostgreSQL", _DB)),
    **_each("npm", "pg postgres @neondatabase @vercel/postgres", Known("PostgreSQL", _DB)),
    **_each("pypi", "pymysql MySQLdb mysql.connector aiomysql", Known("MySQL", _DB)),
    **_each("npm", "mysql mysql2", Known("MySQL", _DB)),
    **_each("pypi", "sqlite3 aiosqlite", Known("SQLite", _DB)),
    **_each("npm", "better-sqlite3 sqlite3", Known("SQLite", _DB)),
    **_each("pypi", "pymongo motor mongoengine beanie", Known("MongoDB", _DB)),
    **_each("npm", "mongodb mongoose", Known("MongoDB", _DB)),
    **_each("pypi", "redis aioredis", Known("Redis", _CACHE)),
    **_each("npm", "redis ioredis @upstash/redis", Known("Redis", _CACHE)),
    ("pypi", "elasticsearch"): Known("Elasticsearch", "search"),
    ("npm", "@elastic/elasticsearch"): Known("Elasticsearch", "search"),
    ("pypi", "pinecone"): Known("Pinecone", "search"),
    ("npm", "@pinecone-database/pinecone"): Known("Pinecone", "search"),
    ("pypi", "qdrant_client"): Known("Qdrant", "search"),
    ("pypi", "chromadb"): Known("Chroma", "search"),
    ("pypi", "weaviate"): Known("Weaviate", "search"),
    ("pypi", "supabase"): Known("Supabase", _DB),
    ("npm", "@supabase"): Known("Supabase", _DB),
    ("pypi", "firebase_admin"): Known("Firebase", _DB),
    ("npm", "firebase"): Known("Firebase", _DB),
    ("npm", "firebase-admin"): Known("Firebase", _DB),
    ("pypi", "google.cloud.storage"): Known("Cloud Storage", "storage"),
    ("npm", "@aws-sdk/client-s3"): Known("Amazon S3", "storage"),
    ("pypi", "boto3"): Known("AWS", "cloud"),
    ("pypi", "minio"): Known("Object storage", "storage"),
    ("pypi", "celery"): Known("Celery", _QUEUE),
    ("pypi", "rq"): Known("RQ", _QUEUE),
    **_each("pypi", "kafka aiokafka confluent_kafka", Known("Kafka", _QUEUE)),
    ("npm", "kafkajs"): Known("Kafka", _QUEUE),
    **_each("pypi", "pika aio_pika", Known("RabbitMQ", _QUEUE)),
    ("npm", "amqplib"): Known("RabbitMQ", _QUEUE),
    **_each("npm", "bullmq bull", Known("BullMQ", _QUEUE)),
    ("pypi", "stripe"): Known("Stripe", "payments"),
    ("npm", "stripe"): Known("Stripe", "payments"),
    ("npm", "@stripe"): Known("Stripe", "payments"),
    ("pypi", "openai"): Known("OpenAI", _AI),
    ("npm", "openai"): Known("OpenAI", _AI),
    ("pypi", "anthropic"): Known("Anthropic", _AI),
    ("npm", "@anthropic-ai"): Known("Anthropic", _AI),
    ("pypi", "google.generativeai"): Known("Gemini", _AI),
    ("pypi", "google.genai"): Known("Gemini", _AI),
    ("pypi", "cohere"): Known("Cohere", _AI),
    ("pypi", "voyageai"): Known("Voyage AI", _AI),
    ("pypi", "twilio"): Known("Twilio", "messaging"),
    ("pypi", "slack_sdk"): Known("Slack", "messaging"),
    ("npm", "@slack"): Known("Slack", "messaging"),
    ("pypi", "sendgrid"): Known("SendGrid", "email"),
    ("npm", "@sendgrid"): Known("SendGrid", "email"),
    ("pypi", "resend"): Known("Resend", "email"),
    ("npm", "resend"): Known("Resend", "email"),
    **_each("pypi", "smtplib aiosmtplib emails fastapi_mail", Known("Email (SMTP)", "email")),
    ("npm", "nodemailer"): Known("Email (SMTP)", "email"),
    ("pypi", "sentry_sdk"): Known("Sentry", "monitoring"),
    ("npm", "@sentry"): Known("Sentry", "monitoring"),
    ("pypi", "posthog"): Known("PostHog", "monitoring"),
    ("npm", "posthog-js"): Known("PostHog", "monitoring"),
    ("npm", "@clerk"): Known("Clerk", "auth"),
    ("npm", "@auth0"): Known("Auth0", "auth"),
    ("pypi", "auth0"): Known("Auth0", "auth"),
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Part:
    """A module the system is built of."""

    id: str  # its folder
    label: str
    tier: Role
    size: int
    members: tuple[Member, ...]
    more: int
    key: tuple[Member, ...] = ()  # its code most joined to other modules'


@dataclass(slots=True)
class Line:
    src: str
    dst: str
    types: Counter[str]
    confidence: float
    # (caller, callee) → how often, and the first place it is seen.
    pairs: Counter[tuple[str, str]] = field(default_factory=Counter[tuple[str, str]])
    seen: dict[tuple[str, str], tuple[str | None, int | None]] = field(
        default_factory=dict[tuple[str, str], tuple[str | None, int | None]]
    )

    def samples(self, limit: int = MAX_SAMPLES) -> tuple[Sample, ...]:
        return tuple(
            Sample(src, dst, *self.seen.get((src, dst), (None, None)))
            for (src, dst), _ in self.pairs.most_common(limit)
        )


@dataclass(frozen=True, slots=True)
class Outside:
    id: str  # "outside:postgresql"
    name: str
    kind: str
    users: Mapping[str, int]  # module → its imports of it


@dataclass(slots=True)
class Facts:
    """What the architecture is drawn from (see the module)."""

    parts: list[Part]
    lines: list[Line]
    outside: list[Outside]
    data_users: Counter[str] = field(default_factory=Counter[str])  # module → its table edges


def _known(ecosystem: str, imported: str) -> Known | None:
    if ecosystem == "pypi":
        parts = imported.split(".")
        for n in range(len(parts), 0, -1):
            found = KNOWN.get((ecosystem, ".".join(parts[:n])))
            if found is not None:
                return found
        return None
    found = KNOWN.get((ecosystem, imported))
    if found is None and imported.startswith("@"):
        found = KNOWN.get((ecosystem, imported.split("/", 1)[0]))
    return found


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


# A fact in no file is in the module of the code it is bound to: an endpoint its handler's, a
# GraphQL field its resolver's, a table its model's.
BOUND_OUT = frozenset({EdgeType.HANDLED_BY, EdgeType.RESOLVES})
BOUND_IN = frozenset({EdgeType.MAPS_TO})


def _home(graph: GraphSnapshot, member: list[str | None], i: int) -> str | None:
    """The module of the code a fact in no file is bound to (see :data:`BOUND_OUT`)."""
    for _, there, index in graph.graph.out_edges(i):
        if graph.edges[index].type in BOUND_OUT and not str(member[there]).startswith("~"):
            return member[there]
    for there, _, index in graph.graph.in_edges(i):
        if graph.edges[index].type in BOUND_IN and not str(member[there]).startswith("~"):
            return member[there]
    return None


def modules_of(graph: GraphSnapshot, roots: Grouping) -> list[str | None]:
    """Each node's module (by position): its folder's, or for a fact in no file the module of
    the code it is bound to (None: none, or outside the repository)."""
    member = [group_of(node, "module", roots) for node in graph.nodes]
    for i, group in enumerate(member):
        if group is not None and group.startswith("~"):
            member[i] = _home(graph, member, i)
    return member


API_LAYERS = frozenset({"route", "handler"})
ENDPOINTS = frozenset({"endpoint"})


def _tier(counts: Counter[str], *, serves: bool = True) -> Role:
    """The tier most of a module's code is in; the API's only for one that serves endpoints (a
    frontend folder named ``api`` is the frontend's)."""
    ranked = sorted(
        (
            (n, layer)
            for layer, n in counts.items()
            if layer != "other" and (serves or layer not in API_LAYERS)
        ),
        key=lambda x: (-x[0], x[1]),
    )
    return TIER_OF.get(ranked[0][1], "other") if ranked else "other"


def _parts(graph: GraphSnapshot, member: list[str | None]) -> list[Part]:
    """The largest modules, each in its tier, with what it holds."""
    sizes: Counter[str] = Counter()
    layers: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for node, group in zip(graph.nodes, member, strict=True):
        if group is not None:
            sizes[group] += 1
            layers[group][layer_of(node, None)] += 1
    tiers: dict[str, Role] = {group: _tier(layers[group]) for group in sizes}
    ranked = sorted(sizes, key=lambda group: (-sizes[group], group))
    layered = [group for group in ranked if tiers[group] != "other"]
    chosen = (layered if len(layered) >= MIN_PARTS_IN_LAYERS else ranked)[:MAX_PARTS]
    kept = set(chosen)
    members: defaultdict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for node, group in zip(graph.nodes, member, strict=True):
        rank = RANK.get(node.kind)
        if rank is not None and group in kept:
            members[group].append((rank, node.name, SAID[node.kind]))
    parts: list[Part] = []
    for group in chosen:
        listed = sorted(set(members[group]))
        tier = tiers[group]
        if tier == "api" and not any(kind in ENDPOINTS for _, _, kind in listed):
            tier = _tier(layers[group], serves=False)
        parts.append(
            Part(
                group, group, tier, sizes[group],
                tuple(Member(name, kind) for _, name, kind in listed[:MAX_MEMBERS]),
                max(0, len(listed) - MAX_MEMBERS),
            )
        )  # fmt: skip
    return parts


def _counts(edge: Edge) -> bool:
    return (
        edge.type in DEPENDS
        and edge.provenance is not Provenance.LLM_INFERRED
        and edge.confidence >= MIN_CONF
    )


def _lines(graph: GraphSnapshot, member: list[str | None], kept: set[str]) -> list[Line]:
    """The graph's edges between the kept modules, counted by type."""
    joined: dict[tuple[str, str], Line] = {}
    for a, b, index in graph.graph.weighted_edge_list():
        edge = graph.edges[index]
        src, dst = member[a], member[b]
        if src is None or dst is None or src == dst or not _counts(edge):
            continue
        if src not in kept or dst not in kept:
            continue
        line = joined.get((src, dst))
        if line is None:
            line = joined[(src, dst)] = Line(src, dst, Counter(), 0.0)
        line.types[edge.type.value] += 1
        line.confidence = max(line.confidence, edge.confidence)
        pair = (_said(graph.nodes[a]), _said(graph.nodes[b]))
        line.pairs[pair] += 1
        line.seen.setdefault(pair, (edge.evidence_path, edge.evidence_line))
    return sorted(joined.values(), key=lambda line: (-sum(line.types.values()), line.src, line.dst))


def _said(node: Node) -> str:
    """A node as a line's sample says it: its qualified name (``ItemService.create``)."""
    return (node.qualified_name or node.name).rstrip(".")[: MAX_NAME * 2]


def _key_code(
    graph: GraphSnapshot, member: list[str | None], kept: set[str]
) -> dict[str, tuple[Member, ...]]:
    """Each kept module's code most joined to other modules' (a method by its class): what the
    module is, for whoever opens it."""
    weight: defaultdict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for a, b, index in graph.graph.weighted_edge_list():
        src, dst = member[a], member[b]
        if src == dst or not _counts(graph.edges[index]):
            continue
        for at, module in ((a, src), (b, dst)):
            if module not in kept:
                continue
            node = graph.nodes[at]
            if node.kind not in KEY_KINDS:
                continue
            name, kind = node.qualified_name or node.name, node.kind
            if kind == "method" and "." in name.rstrip("."):
                name, kind = name.rstrip(".").rsplit(".", 1)[0], "class"
            weight[module or ""][(name.rstrip(".")[: MAX_NAME * 2], kind)] += 1
    return {
        module: tuple(Member(name, kind) for (name, kind), _ in held.most_common(MAX_KEY))
        for module, held in weight.items()
    }


def _outside(graph: GraphSnapshot, kept: set[str]) -> list[Outside]:
    """The outside systems the kept modules import (see :data:`KNOWN`)."""
    by_name: dict[str, tuple[Known, Counter[str]]] = {}
    for (ecosystem, imported), used in usage(graph).items():
        known = _known(ecosystem, imported)
        if known is None:
            continue
        held = by_name.setdefault(known.name, (known, Counter[str]()))
        for path, count in used.files.items():
            module = path.rpartition("/")[0] or "."
            if module in kept:
                held[1][module] += count
    found = [
        Outside(f"outside:{_slug(name)}", name, known.kind, dict(users))
        for name, (known, users) in by_name.items()
        if users
    ]
    found.sort(key=lambda o: (-sum(o.users.values()), o.name))
    return found[:MAX_OUTSIDE]


def facts_of(graph: GraphSnapshot, roots: Grouping) -> Facts:
    """The modules, their lines and the outside systems (see the module)."""
    member = modules_of(graph, roots)
    found = _parts(graph, member)
    kept = {part.id for part in found}
    keys = _key_code(graph, member, kept)
    parts = [replace(part, key=keys.get(part.id, ())) for part in found]
    data_users: Counter[str] = Counter()
    tables = {NodeKind.TABLE.value, NodeKind.ORM_MODEL.value}
    for a, b, index in graph.graph.weighted_edge_list():
        if graph.edges[index].type not in (EdgeType.READS, EdgeType.WRITES):
            continue
        module = member[a]
        if graph.nodes[b].kind in tables and module in kept:
            data_users[module] += 1
    return Facts(parts, _lines(graph, member, kept), _outside(graph, kept), data_users)


# ---------------------------------------------------------------- the model's grouping


class DraftComponent(BaseModel):
    name: str = Field(max_length=200)
    role: str
    icon: str
    description: str = Field(max_length=400)
    modules: list[str]


class Draft(BaseModel):
    name: str = Field(max_length=200)
    summary: str = Field(max_length=1000)
    components: list[DraftComponent]


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    role: Role
    icon: str
    note: str | None
    modules: tuple[str, ...]


def answer_schema() -> dict[str, Any]:
    component = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "role": {"type": "string", "enum": list(ROLES)},
            "icon": {"type": "string", "enum": list(ICONS)},
            "description": {"type": "string"},
            "modules": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "role", "icon", "description", "modules"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "summary": {"type": "string"},
            "components": {"type": "array", "items": component},
        },
        "required": ["name", "summary", "components"],
        "additionalProperties": False,
    }


def system_prompt() -> str:
    path = resources.files("sensei_engine.llm").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


def render(facts: Facts, repo: str) -> str:
    """What the model is shown: the modules, their lines and the outside systems."""
    out = [f"Repository: {repo}", "", "Modules (id [tier] size: what it holds):"]
    for part in facts.parts:
        held = ", ".join(f"{m.kind} {m.name}" for m in part.members)
        more = f" and {part.more} more" if part.more else ""
        key = "; its main code: " + ", ".join(m.name for m in part.key) if part.key else ""
        out.append(f"{part.id} [{part.tier}] {part.size}: {held or '(code)'}{more}{key}")
    out += ["", "Dependencies (from -> to: what):"]
    for line in facts.lines[:MAX_LINES]:
        said = ", ".join(f"{n} {t}" for t, n in line.types.most_common(3))
        out.append(f"{line.src} -> {line.dst}: {said}")
    if facts.outside:
        out += ["", "Outside systems its code uses (name, kind: the modules importing it):"]
        out += [f"{o.name}, {o.kind}: {', '.join(sorted(o.users))}" for o in facts.outside]
    if facts.data_users:
        out += [
            "",
            "Modules reading and writing its tables: " + ", ".join(sorted(facts.data_users)),
        ]
    return "\n".join(out)


def _clean(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit].strip()


def _role(text: str) -> Role:
    for role in ROLES:
        if role == text:
            return role
    return "other"


def check(draft: Draft, facts: Facts) -> tuple[list[Component], list[str]]:
    """The model's components, checked: what it got wrong is said, and left out."""
    known = {part.id for part in facts.parts}
    seen: set[str] = set()
    problems: list[str] = []
    found: list[Component] = []
    for raw in draft.components[:MAX_COMPONENTS]:
        name = _clean(raw.name, MAX_NAME)
        modules: list[str] = []
        for module in raw.modules:
            if module not in known:
                problems.append(f"{name}: {module!r} is not one of the module ids")
            elif module in seen:
                problems.append(f"{name}: {module!r} is in another component already")
            else:
                seen.add(module)
                modules.append(module)
        if not name or "/" in name:
            problems.append(f"{raw.name!r}: name a component as people speak of it, not a path")
            continue
        if not modules:
            continue
        role = _role(raw.role)
        icon = raw.icon if raw.icon in ICONS else ROLE_ICON[role]
        note = _clean(raw.description, MAX_NOTE) or None
        found.append(Component(name, role, icon, note, tuple(modules)))
    missing = sorted(known - seen)
    if missing:
        problems.append("these modules are in no component: " + ", ".join(missing))
    if len(draft.components) > MAX_COMPONENTS:
        problems.append(f"draw at most {MAX_COMPONENTS} components")
    return found, problems


SHOUTED = frozenset({"api", "ui", "db", "mcp", "sdk", "cli", "auth"})


def _humane(label: str) -> str:
    """A folder's name as a part's: ``api/routes`` is "Routes", ``user_service`` "User service",
    ``ui`` "UI"."""
    last = label.rstrip("/").rsplit("/", 1)[-1] or label
    words = re.sub(r"[_\-.]+", " ", last).strip().split()
    if not words:
        return label[:MAX_NAME]
    said = [w.upper() if w.lower() in SHOUTED - {"auth"} else w for w in words]
    said[0] = said[0][:1].upper() + said[0][1:]
    return " ".join(said)[:MAX_NAME]


def plain(facts: Facts, left: Iterable[str] | None = None) -> list[Component]:
    """A part per module (the largest :data:`MAX_PLAIN`, or those in ``left``), named by its
    folder: the grouping drawn without a model."""
    wanted = set(left) if left is not None else None
    parts = [p for p in facts.parts if wanted is None or p.id in wanted]
    if wanted is None:
        parts = parts[:MAX_PLAIN]
    names: Counter[str] = Counter(_humane(p.label) for p in parts)
    return [
        Component(
            _humane(p.label) if names[_humane(p.label)] == 1 else p.label[:MAX_NAME],
            p.tier, ROLE_ICON[p.tier], None, (p.id,),
        )
        for p in parts
    ]  # fmt: skip


# ---------------------------------------------------------------- the spec


def _verb(types: Counter[str]) -> str:
    kind = types.most_common(1)[0][0]
    total = sum(types.values())
    verb = VERBS.get(kind, kind.replace("_", " "))
    return f"{total} {verb}" if total > 1 else verb


def _part_nodes(
    facts: Facts, components: Sequence[Component]
) -> tuple[list[SpecNode], dict[str, str]]:
    """A node per component, and each module's component (its node's id)."""
    parts = {p.id: p for p in facts.parts}
    owner: dict[str, str] = {}
    nodes: list[SpecNode] = []
    ids: Counter[str] = Counter()
    for component in components:
        base = f"part:{_slug(component.name)}"
        ids[base] += 1
        key = base if ids[base] == 1 else f"{base}-{ids[base]}"
        for module in component.modules:
            owner[module] = key
        # Its folders, then what they hold (endpoints, tables, components) and their main code.
        held = [Member(module, "module") for module in component.modules]
        inside = [
            m for module in component.modules if module in parts for m in parts[module].members
        ]
        inside += [m for module in component.modules if module in parts for m in parts[module].key]
        listed = list(dict.fromkeys([*held, *inside]))
        nodes.append(
            SpecNode(
                key, component.name, component.role, component.role, None, 0, 0,
                tuple(listed[:MAX_PART_MEMBERS]), max(0, len(listed) - MAX_PART_MEMBERS),
                note=component.note, icon=component.icon,
                size=sum(parts[m].size for m in component.modules if m in parts),
            )
        )  # fmt: skip
    return nodes, owner


def _part_lines(facts: Facts, owner: Mapping[str, str]) -> list[SpecEdge]:
    """The modules' lines between components, joined and counted (the graph's: verified)."""
    joined: dict[tuple[str, str], Line] = {}
    for line in facts.lines:
        src, dst = owner.get(line.src), owner.get(line.dst)
        if src is None or dst is None or src == dst:
            continue
        held = joined.get((src, dst))
        if held is None:
            held = joined[(src, dst)] = Line(src, dst, Counter(), 0.0)
        held.types += line.types
        held.confidence = max(held.confidence, line.confidence)
        held.pairs += line.pairs
        for pair, where in line.seen.items():
            held.seen.setdefault(pair, where)
    ranked = sorted(joined.values(), key=lambda line: -sum(line.types.values()))[:MAX_LINES]
    return [
        SpecEdge(
            line.src, line.dst, line.types.most_common(1)[0][0], _verb(line.types), True,
            line.confidence, "graph", None, sum(line.types.values()), line.samples(),
        )
        for line in ranked
    ]  # fmt: skip


def _by_part(counts: Mapping[str, int], owner: Mapping[str, str]) -> Counter[str]:
    found: Counter[str] = Counter()
    for module, count in counts.items():
        if module in owner:
            found[owner[module]] += count
    return found


def _outside_parts(facts: Facts, owner: Mapping[str, str]) -> tuple[list[SpecNode], list[SpecEdge]]:
    """The outside systems the components import, and the database their tables are in: the
    one the code has a driver for, else one by that name."""
    databases = [o for o in facts.outside if o.kind == "database"]
    tables_in = databases[0].id if len(databases) == 1 else None
    shown = list(facts.outside)
    if facts.data_users and tables_in is None:
        tables_in = "outside:database"
        shown.append(Outside(tables_in, "Database", "database", {}))
    nodes: list[SpecNode] = []
    edges: list[SpecEdge] = []
    for outside in shown:
        users = _by_part(outside.users, owner)
        reads = _by_part(facts.data_users, owner) if outside.id == tables_in else Counter[str]()
        if not users and not reads:
            continue
        nodes.append(SpecNode(outside.id, outside.name, "outside", outside.kind, None, 0, 0))
        for part in sorted(set(users) | set(reads)):
            if reads[part]:
                kind, said = "reads", f"{reads[part]} reads and writes"
            else:
                kind, said = "imports", f"{users[part]} imports" if users[part] > 1 else "imports"
            edges.append(SpecEdge(part, outside.id, kind, said, True, 1.0, "graph", None))
    return nodes, edges


def _entries(nodes: Sequence[SpecNode], edges: Sequence[SpecEdge], layer: str) -> list[str]:
    """The parts of a tier nothing else in it reaches (its pages, not the components they
    render); the largest when every one is reached (a cycle)."""
    held = [n for n in nodes if n.layer == layer]
    ids = {n.id for n in held}
    reached = {e.dst for e in edges if e.src in ids and e.dst in ids}
    entries = [n.id for n in held if n.id not in reached]
    if entries or not held:
        return entries
    return [max(held, key=lambda n: (n.size, n.id)).id]


def _people(
    nodes: Sequence[SpecNode], edges: Sequence[SpecEdge]
) -> tuple[SpecNode | None, list[SpecEdge]]:
    """Who uses the system, above it: people its frontend's entries (an inferred line, saying
    why), or clients its API with none."""
    fronts = _entries(nodes, edges, "frontend")
    targets = fronts or _entries(nodes, edges, "api")
    if not targets:
        return None, []
    actor = SpecNode(
        "actor:user", "User" if fronts else "Client", "actor", "actor", None, 0, 0, icon="user"
    )
    why = (
        "people use the app through its pages" if fronts else "clients call its endpoints over HTTP"
    )
    verb = "uses" if fronts else "calls"
    return actor, [SpecEdge(actor.id, t, None, verb, False, None, None, why) for t in targets]


def spec_of(
    facts: Facts, components: Sequence[Component], title: str, summary: str | None
) -> DiagramSpec:
    """The diagram: the people using it, its parts, the outside systems; the lines between.
    Tooling, scripts and docs (role ``other``) are left out of an app that has more."""
    if any(c.role != "other" for c in components):
        components = [c for c in components if c.role != "other"]
    nodes, owner = _part_nodes(facts, components)
    edges = _part_lines(facts, owner)
    outside, used = _outside_parts(facts, owner)
    actor, reach = _people(nodes, edges)
    return DiagramSpec(
        title, "architecture", [*([actor] if actor else []), *nodes, *outside],
        [*reach, *edges, *used], summary=summary,
    )  # fmt: skip


def whole_app(graph: GraphSnapshot, roots: Grouping, title: str) -> DiagramSpec:
    """The architecture without a model: a part per module, named by its folder."""
    facts = facts_of(graph, roots)
    return spec_of(facts, plain(facts), title, None)


# ---------------------------------------------------------------- how it works


class DraftLine(BaseModel):
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    label: str = Field(max_length=200)


class DraftStep(BaseModel):
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    text: str = Field(max_length=1000)


class FlowDraft(BaseModel):
    lines: list[DraftLine]
    steps: list[DraftStep]


def flow_schema() -> dict[str, Any]:
    def pair(extra: str) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "to": {"type": "string"},
                extra: {"type": "string"},
            },
            "required": ["from", "to", extra],
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": {
            "lines": {"type": "array", "items": pair("label")},
            "steps": {"type": "array", "items": pair("text")},
        },
        "required": ["lines", "steps"],
        "additionalProperties": False,
    }


def flow_prompt() -> str:
    path = resources.files("sensei_engine.llm").joinpath("prompts", f"{FLOW_PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


def render_flow(spec: DiagramSpec) -> str:
    """What the model is shown to explain the system: its parts and its lines with the code's
    own edges behind them."""
    out = [f"System: {spec.summary or spec.title}", "", "Parts (id: name [role] what it does):"]
    for node in spec.nodes:
        held = [m.name for m in node.members if m.kind != "module"][:10]
        holds = f"; holds {', '.join(held)}" if held else ""
        out.append(f"{node.id}: {node.label} [{node.layer}] {node.note or node.kind}{holds}")
    out += ["", "Lines (from -> to: how many; some of the code behind it):"]
    for edge in spec.edges:
        code = "; ".join(f"{x.src} → {x.dst}" for x in edge.samples[:3])
        said = edge.inferred or code or (edge.label or "")
        out.append(f"{edge.src} -> {edge.dst}: {edge.count or 1}; {said}")
    return "\n".join(out)


def explain(spec: DiagramSpec, draft: FlowDraft) -> tuple[DiagramSpec, list[str]]:
    """``spec`` with the model's labels on its lines and its walkthrough, checked: a line or a
    step that isn't one of the spec's is left out, and said."""
    lines = {(e.src, e.dst) for e in spec.edges}
    problems: list[str] = []
    labels: dict[tuple[str, str], str] = {}
    for raw in draft.lines:
        key = (raw.source, raw.target)
        label = _clean(raw.label, MAX_PHRASE)
        if key not in lines:
            problems.append(f"{raw.source} -> {raw.target} is not one of the lines")
        elif label:
            labels[key] = label
    steps: list[Step] = []
    for raw in draft.steps[:MAX_STEPS]:
        key = (raw.source, raw.target)
        text = _clean(raw.text, MAX_STEP)
        if key not in lines:
            problems.append(f"step {raw.source} -> {raw.target} is not one of the lines")
        elif text:
            steps.append(Step(raw.source, raw.target, text))
    edges = [replace(e, label=labels.get((e.src, e.dst), e.label)) for e in spec.edges]
    return replace(spec, edges=edges, steps=steps), problems


async def _explained(
    client: AnthropicClient,
    spec: DiagramSpec,
    *,
    tier: Tier,
    effort: Any,
    on_usage: LLMUsageSink | None,
) -> DiagramSpec:
    """The spec with its lines said and its walkthrough (as it was, when the model fails)."""
    if not spec.edges:
        return spec
    request = LLMRequest(
        render_flow(spec), system=flow_prompt(), tier=tier, max_tokens=MAX_TOKENS,
        effort=effort, schema=flow_schema(), cache_system=True,
    )  # fmt: skip
    try:
        result = await client.complete(request, on_usage=on_usage)
        draft = FlowDraft.model_validate(result.data)
    except (LLMError, ValidationError) as exc:
        log.warning("architecture walkthrough unusable", error=str(exc))
        return spec
    explained, problems = explain(spec, draft)
    if problems:
        log.info("architecture walkthrough checked", problems=len(problems))
    return explained


@dataclass(frozen=True, slots=True)
class Drawn:
    spec: DiagramSpec
    tries: int  # the model's answers read (0: drawn without one)
    problems: list[str]


async def draw(
    client: AnthropicClient | None,
    facts: Facts,
    title: str,
    repo: str,
    *,
    on_usage: LLMUsageSink | None = None,
    floor: StepChoice | None = None,
    max_retries: int = MAX_RETRIES,
) -> Drawn:
    """The architecture, its parts grouped and named by the model (see the module)."""
    if not facts.parts:
        return Drawn(spec_of(facts, [], title, None), 0, [])
    if client is None or not client.configured:
        return Drawn(spec_of(facts, plain(facts), title, None), 0, [])
    tier, effort = Tier.STANDARD, "low"
    if floor is not None:
        tier, effort = at_floor(floor, tier, effort)
    base = render(facts, repo)
    feedback: list[str] = []
    best: tuple[list[Component], str | None] | None = None
    problems: list[str] = []
    tries = 0
    for _ in range(1 + max_retries):
        prompt = base
        if feedback:
            prompt += "\n\nYour last answer had these problems; answer again:\n" + "\n".join(
                f"- {p}" for p in feedback
            )
        request = LLMRequest(
            prompt, system=system_prompt(), tier=tier, max_tokens=MAX_TOKENS, effort=effort,
            schema=answer_schema(), cache_system=True,
        )  # fmt: skip
        try:
            result = await client.complete(request, on_usage=on_usage)
            draft = Draft.model_validate(result.data)
        except (LLMError, ValidationError) as exc:
            log.warning("architecture answer unusable", error=str(exc))
            break
        tries += 1
        components, problems = check(draft, facts)
        best = (components, _clean(draft.summary, MAX_SUMMARY) or None)
        if not problems:
            break
        feedback = problems
    if best is None or not best[0]:
        return Drawn(spec_of(facts, plain(facts), title, None), tries, problems)
    components, summary = best
    placed = {m for c in components for m in c.modules}
    left = [p.id for p in facts.parts if p.id not in placed]
    if left:
        components = [*components, *plain(facts, left)]
    spec = spec_of(facts, components, title, summary)
    spec = await _explained(client, spec, tier=tier, effort=effort, on_usage=on_usage)
    return Drawn(spec, tries, problems)
