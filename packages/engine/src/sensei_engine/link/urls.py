"""Frontend HTTP calls to backend endpoints (3.10): URL normalization, a segment trie, and
``TARGETS`` edges.

**Normalizing** (:func:`normalize`): the method upper-cased; scheme, host, query and fragment
dropped; a known base path applied (an ``axios.create({baseURL})``, a Vite proxy or Next
rewrite, FastAPI's ``root_path``); every parameter syntax (``{id}``, ``:id``, ``<int:pk>``,
``[id]``, ``${x}``) becomes ``{}`` and a catch-all (``[...slug]``, ``{x:path}``, ``*``) ``{*}``;
no trailing slash. An endpoint is keyed by the same form (:func:`sensei_engine.link.fastapi.
normalize_path`).

**Matching** (:class:`EndpointTrie`): per method, segment by segment, each endpoint at its path
and, when its app has a ``root_path`` (a proxy's prefix), under it as well. A literal segment of the
call matches the same literal, or a parameter; a parameter of the call matches only a parameter
(its value is unknown, so it can't be a fixed segment); ``{*}`` takes the rest. Of the matches,
the most literal win (``/users/me`` over ``/users/{}``); several left are ambiguous.

**Confidence** (``TARGETS``):

- an operationId the call names (a generated client's, 3.9) that one endpoint of the call's
  method has: ``contract``, 1.0. The two are compared without case or punctuation: a generator
  writes the operationId ``login-login_access_token`` as ``loginLoginAccessToken``. When the
  call's URL matches endpoints, that one must be among them (a stale client's operationId
  doesn't outrank its URL);
- one match, every segment matched its own kind (a literal the same literal, a parameter a
  parameter: ``/items/${id}`` to ``/items/{id}``) and the base known (the call's own, or one the
  config adds; not dropped for the match): ``url_literal`` raised to 0.9;
- one match otherwise: ``url_literal``'s 0.5; ``k`` matches: 0.5/k each.

A call that matches nothing gets a reason for the trace's "gap" (the report's, and on its node:
:func:`with_reasons` sets ``attrs.unmatched_reason``): the path exists under another method, the
URL is dynamic, its base is unknown, or no endpoint has it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.link.http_calls import HttpCall

PARAM = "{}"
REST = "{*}"
LITERAL_CONFIDENCE = 0.9  # one exact match, the base known
MAX_CANDIDATES = 8

_CATCH_ALL_RE = re.compile(r"^(?:\[\[?\.\.\.[^\]]+\]\]?|\{[^{}]*:path\}|\*\*?|:[A-Za-z_]\w*\*)$")
_PARAM_RE = re.compile(
    r"^(?:\{[^{}]*\}|:[A-Za-z_]\w*\??|<(?:[A-Za-z_]\w*:)?[A-Za-z_]\w*>|\[[^\]]+\]|\$\{[^}]*\})$"
)
_ENV_RE = re.compile(r"^\{env:[^}]+\}$")
_INLINE_PARAM_RE = re.compile(r"\$\{[^}]*\}|\{[^{}]*\}")  # inside a segment: "v{}" / "item-${id}"


@dataclass(frozen=True, slots=True)
class Normalized:
    method: str
    path: str  # "/api/items/{}"
    segments: tuple[str, ...]
    literal: bool  # every segment a literal
    base_unknown: bool  # it starts with an environment value or a base nobody resolved


def _segment(raw: str) -> str:
    if _CATCH_ALL_RE.match(raw):
        return REST
    if _PARAM_RE.match(raw):
        return PARAM
    if _INLINE_PARAM_RE.search(raw):
        return PARAM  # part literal, part value: matched as a parameter
    return raw


def normalize(method: str, url: str, *, base: str = "") -> Normalized:
    """``url`` (a path, a full URL, or a template with ``{}``/``${x}`` holes) as matched."""
    text = url.strip()
    if "://" in text or text.startswith("//"):
        parts = urlsplit(text if "://" in text else "http:" + text)
        text = parts.path or "/"
    text = re.split(r"[?#]", text, maxsplit=1)[0]
    raw_segments = [s for s in text.split("/") if s]
    base_unknown = bool(raw_segments) and bool(_ENV_RE.match(raw_segments[0]))
    if base_unknown:
        raw_segments = raw_segments[1:]
    base_segments = [s for s in base.split("/") if s] if base else []
    segments = tuple(_segment(s) for s in [*base_segments, *raw_segments])
    if REST in segments:
        segments = segments[: segments.index(REST) + 1]
    path = "/" + "/".join(segments)
    return Normalized(
        method=method.upper(),
        path=path,
        segments=segments,
        literal=all(s not in (PARAM, REST) for s in segments),
        base_unknown=base_unknown,
    )


@dataclass(slots=True)
class _Node:
    literals: dict[str, _Node] = field(default_factory=dict[str, "_Node"])
    param: _Node | None = None
    rest: list[str] = field(default_factory=list[str])  # endpoints ending in {*} here
    ends: list[str] = field(default_factory=list[str])  # endpoint keys ending here


@dataclass(frozen=True, slots=True)
class Match:
    endpoint: str
    literals: int  # segments matched literal to literal
    exact: bool  # every segment matched its own kind (literal to literal, parameter to one)


class EndpointTrie:
    """Endpoints by method and path segments (see the module)."""

    def __init__(self) -> None:
        self._roots: dict[str, _Node] = defaultdict(_Node)
        self.paths: dict[str, set[str]] = defaultdict(set)  # path -> methods, for the reasons

    def add(self, method: str, path: str, key: str) -> None:
        segments = [s for s in path.split("/") if s]
        node = self._roots[method.upper()]
        for segment in segments:
            if segment == REST:
                node.rest.append(key)
                break
            if segment == PARAM:
                node.param = node.param or _Node()
                node = node.param
            else:
                node = node.literals.setdefault(segment, _Node())
        else:
            node.ends.append(key)
        self.paths["/" + "/".join(segments)].add(method.upper())

    def match(self, call: Normalized) -> list[Match]:
        """Every endpoint ``call`` can mean, the most literal first (ties: all of them)."""
        root = self._roots.get(call.method)
        if root is None:
            return []
        found: list[Match] = []
        # node, segment index, literals matched, exact so far
        stack: list[tuple[_Node, int, int, bool]] = [(root, 0, 0, True)]
        segments = call.segments
        while stack and len(found) < 256:
            node, index, literals, exact = stack.pop()
            found += [Match(key, literals, False) for key in node.rest]
            if index == len(segments):
                found += [Match(key, literals, exact) for key in node.ends]
                continue
            segment = segments[index]
            if segment == REST:
                continue  # the call's catch-all matches only an endpoint's (above)
            if segment != PARAM and segment in node.literals:
                stack.append((node.literals[segment], index + 1, literals + 1, exact))
            if node.param is not None:
                stack.append((node.param, index + 1, literals, exact and segment == PARAM))
        if not found:
            return []
        best = max(m.literals for m in found)
        kept = {m.endpoint: m for m in found if m.literals == best}
        return sorted(kept.values(), key=lambda m: m.endpoint)

    def methods_for(self, call: Normalized) -> set[str]:
        """The methods some endpoint has at ``call``'s path (for "method differs")."""
        found: set[str] = set()
        for method in list(self._roots):
            if method != call.method:
                probe = Normalized(method, call.path, call.segments, call.literal, False)
                if self.match(probe):
                    found.add(method)
        return found


@dataclass(frozen=True, slots=True)
class Endpoint:
    key: str  # its node key
    method: str
    path_norm: str
    operation_id: str | None = None
    root_path: str = ""  # its app's (a proxy's prefix, "/api"): it's matched under it too


def operation_key(operation_id: str) -> str:
    """An operationId as generators and FastAPI both write it: no case, no punctuation."""
    return re.sub(r"[^a-z0-9]", "", operation_id.lower())


@dataclass(slots=True)
class LinkReport:
    calls: int = 0
    linked: int = 0
    contract: int = 0
    ambiguous: int = 0
    unmatched: dict[str, str] = field(default_factory=dict[str, str])  # call key -> reason


@dataclass(slots=True)
class Linked:
    edges: list[GraphEdge]
    report: LinkReport


def _base_variants(call: HttpCall, bases: Sequence[str]) -> list[tuple[str, bool]]:
    """The bases to try: the call's own, none, then every known one (rewrites, proxies,
    root_path); and whether that base is a resolved one."""
    found: list[tuple[str, bool]] = [(call.base, True)]
    for base in ("", *bases):
        if base != call.base and (base, True) not in found:
            found.append((base, bool(base)))
    return found


def _url_matches(
    trie: EndpointTrie, call: HttpCall, bases: Sequence[str]
) -> tuple[Normalized, list[Match], bool]:
    """The call as matched, the endpoints its URL can mean, and whether its base is known
    (its own or a config's, and no environment value before it)."""
    normalized = normalize(call.method, call.url)
    if call.dynamic:
        return normalized, [], False
    for base, known in _base_variants(call, bases):
        normalized = normalize(call.method, call.url, base=base)
        matches = trie.match(normalized)
        if matches:
            return normalized, matches, known and not normalized.base_unknown
    return normalized, [], False


def link_calls(
    calls: Iterable[HttpCall], endpoints: Iterable[Endpoint], *, bases: Sequence[str] = ()
) -> Linked:
    """``TARGETS`` edges from calls to the endpoints they mean (see the module); ``bases``: the
    base paths the repository's config adds (``/api`` a proxy forwards, a ``root_path``)."""
    trie = EndpointTrie()
    by_operation: dict[tuple[str, str], list[str]] = defaultdict(list)  # (method, key) -> keys
    for endpoint in endpoints:
        trie.add(endpoint.method, endpoint.path_norm, endpoint.key)
        if endpoint.root_path.strip("/"):
            under = normalize(endpoint.method, endpoint.path_norm, base=endpoint.root_path).path
            trie.add(endpoint.method, under, endpoint.key)
        if endpoint.operation_id:
            operation = (endpoint.method.upper(), operation_key(endpoint.operation_id))
            by_operation[operation].append(endpoint.key)
    edges: list[GraphEdge] = []
    report = LinkReport()
    for call in calls:
        report.calls += 1
        normalized, matches, resolved = _url_matches(trie, call, bases)
        contract = (
            by_operation.get((call.method.upper(), operation_key(call.operation_id)), [])
            if call.operation_id
            else []
        )
        if len(contract) == 1 and (
            not matches or contract[0] in {match.endpoint for match in matches}
        ):
            edges.append(
                GraphEdge(
                    call.key,
                    contract[0],
                    EdgeType.TARGETS,
                    Provenance.CONTRACT,
                    confidence=confidence_of(Provenance.CONTRACT),
                    attrs={"via": "operation_id", "operation_id": call.operation_id},
                    evidence_path=call.evidence_path,
                    evidence_line=call.evidence_line,
                    owner=call.owner,
                )
            )
            report.linked += 1
            report.contract += 1
            continue
        if call.dynamic:
            report.unmatched[call.key] = "the URL is built at run time (no literal to match)"
            continue
        if not matches:
            report.unmatched[call.key] = _reason(trie, normalized)
            continue
        matches = matches[:MAX_CANDIDATES]
        if len(matches) == 1:
            sure = matches[0].exact and resolved
            confidence = LITERAL_CONFIDENCE if sure else confidence_of(Provenance.URL_LITERAL)
        else:
            confidence = confidence_of(Provenance.URL_LITERAL) / len(matches)
            report.ambiguous += 1
        for match in matches:
            edges.append(
                GraphEdge(
                    call.key,
                    match.endpoint,
                    EdgeType.TARGETS,
                    Provenance.URL_LITERAL,
                    confidence=confidence,
                    attrs={"path": normalized.path, "candidates": len(matches)},
                    evidence_path=call.evidence_path,
                    evidence_line=call.evidence_line,
                    owner=call.owner,
                )
            )
        report.linked += 1
    return Linked(edges, report)


def with_reasons(nodes: Iterable[GraphNode], report: LinkReport) -> list[GraphNode]:
    """The call nodes, each that matched nothing with its reason (``attrs.unmatched_reason``)."""
    return [
        replace(node, attrs={**node.attrs, "unmatched_reason": report.unmatched[node.key]})
        if node.key in report.unmatched
        else node
        for node in nodes
    ]


def _reason(trie: EndpointTrie, call: Normalized) -> str:
    other = trie.methods_for(call)
    if other:
        return f"no {call.method} endpoint at {call.path} (it has {', '.join(sorted(other))})"
    if call.base_unknown:
        return f"its base URL comes from the environment; no endpoint at {call.path}"
    return f"no endpoint matches {call.method} {call.path}"


def config_bases(configs: Mapping[str, str]) -> list[str]:
    """Base paths a frontend's config forwards: the keys of a Vite ``server.proxy``
    (``'/api': ...``) and Next.js rewrites' sources (``source: '/api/:path*'``), as prefixes."""
    found: list[str] = []
    for path, text in configs.items():
        name = path.rsplit("/", 1)[-1]
        if name.startswith("vite.config"):
            block = re.search(r"proxy\s*:\s*\{(.*?)\}\s*,?\s*\}", text, re.DOTALL)
            keys = re.findall(r"""['"](/[^'"]*)['"]\s*:""", block[1] if block else "")
            found += keys
        elif name.startswith("next.config"):
            found += re.findall(r"""source\s*:\s*['"](/[^'":*]*)""", text)
    cleaned = {
        "/" + "/".join(s for s in base.split("/") if s and not s.startswith(":")) for base in found
    }
    return sorted(b for b in cleaned if b != "/")
