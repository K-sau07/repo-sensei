"""Frontend HTTP calls to backend endpoints (3.10): URL normalization, a segment trie, and
``TARGETS`` edges.

**Normalizing** (:func:`normalize`): the method upper-cased; scheme, host, query and fragment
dropped; a known base path applied (an ``axios.create({baseURL})``, a Vite proxy or Next
rewrite, FastAPI's ``root_path``); every parameter syntax (``{id}``, ``:id``, ``<int:pk>``,
``[id]``, ``${x}``) becomes ``{}`` and a catch-all (``[...slug]``, ``{x:path}``, ``*``) ``{*}``;
no trailing slash. What comes before the first slash when it is an environment value
(``{env:API_URL}/items``) or a value not worked out (``${base}/items``: an origin, a base path
read at run time) is an unknown base, dropped. An endpoint is keyed by the same form
(:func:`sensei_engine.link.fastapi.normalize_path`).

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
- one match otherwise: ``url_literal``'s 0.5; ``k`` matches: 0.5/k each;
- a match only with a base path moved (:func:`_prefix_matches`), when nothing matches as it is:
  every route of a server under a prefix the call doesn't have (controllers all under
  ``/api/...`` while the client's base URL is the bare origin), the rest of the prefix added
  after what the call has of it; or a
  first segment of the call that no endpoint starts with (or a proxy's prefix, which it may
  strip), dropped. Only for a call to a relative URL, a local host or an environment's, and
  only when one of the call's own literals still matches: :data:`PREFIX_CONFIDENCE` (0.3), /k
  for ``k``, and the edge says what was moved and why (``attrs.prefix_added`` or
  ``prefix_dropped``, ``attrs.reason``).

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
PREFIX_CONFIDENCE = 0.3  # a match only with a base path added or dropped (see the module)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})  # noqa: S104
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


def _url_path(url: str) -> str:
    """A full URL's path; one ``urlsplit`` can't read (``http://[::1/x``, a bracket never
    closed) is cut after its host by hand."""
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        rest = url.split("://", 1)[-1]
        return "/" + rest.split("/", 1)[1] if "/" in rest else "/"


def normalize(method: str, url: str, *, base: str = "") -> Normalized:
    """``url`` (a path, a full URL, or a template with ``{}``/``${x}`` holes) as matched."""
    text = url.strip()
    if "://" in text or text.startswith("//"):
        text = _url_path(text if "://" in text else "http:" + text)
    text = re.split(r"[?#]", text, maxsplit=1)[0]
    raw_segments = [s for s in text.split("/") if s]
    # An environment value, or a value nobody worked out, before the first slash: the base.
    base_unknown = bool(raw_segments) and (
        bool(_ENV_RE.match(raw_segments[0])) or text.startswith(PARAM + "/")
    )
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

    def first_segments(self) -> set[str]:
        """The literal segments some endpoint's path starts with (any method)."""
        return {s for root in self._roots.values() for s in root.literals}

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
    server: str = ""  # the app serving it (a Spring module; "": the one), for its shared prefix


def operation_key(operation_id: str) -> str:
    """An operationId as generators and FastAPI both write it: no case, no punctuation."""
    return re.sub(r"[^a-z0-9]", "", operation_id.lower())


@dataclass(slots=True)
class LinkReport:
    calls: int = 0
    linked: int = 0
    contract: int = 0
    ambiguous: int = 0
    prefixed: int = 0  # linked only with a base path added or dropped
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


def shared_prefix(paths: Iterable[str]) -> tuple[str, ...]:
    """The literal segments every one of ``paths`` starts with."""
    common: list[str] | None = None
    for path in paths:
        segments = [s for s in path.split("/") if s]
        if common is None:
            common = segments
            continue
        size = next(
            (i for i, (a, b) in enumerate(zip(common, segments, strict=False)) if a != b),
            min(len(common), len(segments)),
        )
        common = common[:size]
    kept: list[str] = []
    for segment in common or []:
        if segment in (PARAM, REST):
            break
        kept.append(segment)
    return tuple(kept)


def _local(url: str) -> bool:
    """Whether a call's URL is relative, or to a local host (a server of the repository's)."""
    text = url.strip()
    if "://" not in text and not text.startswith("//"):
        return True
    try:
        host = urlsplit(text if "://" in text else "http:" + text).hostname
    except ValueError:  # http://[::1/x: no host to read
        return False
    return host is None or host in LOCAL_HOSTS


def _moved(call: Normalized, segments: tuple[str, ...]) -> Normalized:
    return Normalized(
        call.method,
        "/" + "/".join(segments),
        segments,
        all(s not in (PARAM, REST) for s in segments),
        call.base_unknown,
    )


def _prefix_matches(
    trie: EndpointTrie,
    call: HttpCall,
    prefixes: Sequence[tuple[str, ...]],
    bases: Sequence[str],
) -> tuple[Normalized, list[Match], dict[str, str]] | None:
    """The endpoints a call means with a base path moved (see the module), and what moved."""
    if not _local(call.url):
        return None
    plain = normalize(call.method, call.url, base=call.base)
    segments = plain.segments
    for prefix in prefixes:
        # What the call has of the prefix already (a context path, but not the "/api" after it).
        have = next(
            (i for i in range(len(prefix)) if segments[i : i + 1] != prefix[i : i + 1]),
            len(prefix),
        )
        if have == len(prefix):
            continue
        moved = _moved(plain, (*prefix, *segments[have:]))
        found = [m for m in trie.match(moved) if m.literals > len(prefix)]
        if found:
            added = "/" + "/".join(prefix[have:])
            reason = f"the call's URL has no {added}, which every route of its server is under"
            return moved, found, {"prefix_added": added, "reason": reason}
    drops = [tuple(s for s in base.split("/") if s) for base in bases]
    if segments and segments[0] not in (PARAM, REST) and segments[0] not in trie.first_segments():
        drops.append(segments[:1])
    for drop in drops:
        if not drop or len(segments) <= len(drop) or segments[: len(drop)] != drop:
            continue
        moved = _moved(plain, segments[len(drop) :])
        found = [m for m in trie.match(moved) if m.literals >= 1]
        if found:
            dropped = "/" + "/".join(drop)
            reason = f"no endpoint is under the call's {dropped}; matched without it"
            return moved, found, {"prefix_dropped": dropped, "reason": reason}
    return None


def link_calls(
    calls: Iterable[HttpCall], endpoints: Iterable[Endpoint], *, bases: Sequence[str] = ()
) -> Linked:
    """``TARGETS`` edges from calls to the endpoints they mean (see the module); ``bases``: the
    base paths the repository's config adds (``/api`` a proxy forwards, a ``root_path``)."""
    trie = EndpointTrie()
    by_operation: dict[tuple[str, str], list[str]] = defaultdict(list)  # (method, key) -> keys
    by_server: dict[str, list[str]] = defaultdict(list)
    for endpoint in endpoints:
        by_server[endpoint.server].append(endpoint.path_norm)
        trie.add(endpoint.method, endpoint.path_norm, endpoint.key)
        if endpoint.root_path.strip("/"):
            under = normalize(endpoint.method, endpoint.path_norm, base=endpoint.root_path).path
            trie.add(endpoint.method, under, endpoint.key)
        if endpoint.operation_id:
            operation = (endpoint.method.upper(), operation_key(endpoint.operation_id))
            by_operation[operation].append(endpoint.key)
    prefixes = sorted(
        {shared_prefix(paths) for paths in by_server.values() if len(paths) > 1} - {()}
    )
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
            moved = _prefix_matches(trie, call, prefixes, bases)
            if moved is None:
                report.unmatched[call.key] = _reason(trie, normalized)
                continue
            edges += _prefixed_edges(call, *moved)
            report.linked += 1
            report.prefixed += 1
            report.ambiguous += len(moved[1][:MAX_CANDIDATES]) > 1
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


def _prefixed_edges(
    call: HttpCall, normalized: Normalized, matches: list[Match], moved: dict[str, str]
) -> list[GraphEdge]:
    kept = matches[:MAX_CANDIDATES]
    return [
        GraphEdge(
            call.key,
            match.endpoint,
            EdgeType.TARGETS,
            Provenance.URL_LITERAL,
            confidence=PREFIX_CONFIDENCE / len(kept),
            attrs={"path": normalized.path, "candidates": len(kept), **moved},
            evidence_path=call.evidence_path,
            evidence_line=call.evidence_line,
            owner=call.owner,
        )
        for match in kept
    ]


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
