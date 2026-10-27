"""Query routing: what kind of question a search query is, and what in it must be kept.

Two steps:

1. :func:`classify` reads the query with rules: pure, instant, deterministic. It collects the
   **literals** (backticked or quoted text, code fragments, regex patterns, CONSTANT_NAMES,
   ``/routes``, file paths), the identifiers, the path filters and the layers a query rules
   out ("(not tests)"). Two classes it decides alone, with no model call:

   * ``symbol``: the query names one definition ("``useFlowStore``", "Vertex class",
     "where is the Graph class defined?");
   * ``regex``: the query looks for a literal or pattern rather than asking how something
     works ("every use of the "refresh_token" cookie", "``TODO\\(auth\\)``").

   For anything else it also guesses a class from cue words (flow, architecture, impact,
   else conceptual), used when the model can't be asked.

2. :class:`QueryRouter` asks Claude (the fast tier, low effort, structured output) for the
   class, the layers involved, likely identifiers and routes, and two rewrites: a structural
   one (which component, route or layer) and a behavioural one (what it does). Answers are
   cached per scope (the org) and normalized query: orgs never share an entry, so a fast
   answer can't tell one org what another asked. The rules' literals always survive: the
   model may add literals only if they are in the query verbatim. If the model fails,
   refuses or is too slow, the rules' answer is used.

Rewrites are additive (the original query always runs as its own legs): what to run is
:func:`sensei_engine.search.plan.plan`'s, running it the retriever's (2.9).
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import cache
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from sensei_core.logging import get_logger
from sensei_engine.llm.anthropic_client import (
    AnthropicClient,
    LLMError,
    LLMRequest,
    LLMUsageSink,
    Role,
    tier_for,
)

log = get_logger(__name__)

PROMPT_VERSION = "route_query.v1"
CACHE_PREFIX = "route:v1:"
CACHE_TTL_SECONDS = 7 * 24 * 3600
TIMEOUT_SECONDS = 8.0
MAX_TOKENS = 2_000  # the answer is ~150 tokens; the rest is room for the model's thinking
MAX_GUESSES = 8  # per list in the model's answer
# The rules read this much of a query (a question is far shorter): their time stays bounded. A
# longer query goes to the model.
MAX_RULE_CHARS = 1_000


class QueryClass(enum.StrEnum):
    SYMBOL = "symbol"  # one definition, by name
    REGEX = "regex"  # a literal or pattern to find
    CONCEPTUAL = "conceptual"  # behaviour described in words
    FLOW = "flow"  # a path across layers: UI -> API -> DB
    ARCHITECTURE = "architecture"  # how a part is structured
    IMPACT = "impact"  # what depends on something, what a change breaks


class Layer(enum.StrEnum):
    UI = "ui"
    STATE = "state"
    CLIENT = "client"
    ROUTE = "route"
    HANDLER = "handler"
    SERVICE = "service"
    DB = "db"
    WORKER = "worker"
    CONFIG = "config"
    TEST = "test"
    DOCS = "docs"


class Lane(enum.StrEnum):
    """The design system's swimlanes, as the API, the constellation and the flow view (2.13)
    draw them. The router's finer layers fold into them (:data:`LANES`)."""

    UI = "ui"
    API = "api"
    SVC = "svc"
    DB = "db"
    OTHER = "other"


LANES: Mapping[Layer, Lane] = {
    Layer.UI: Lane.UI,
    Layer.STATE: Lane.UI,
    Layer.CLIENT: Lane.UI,
    Layer.ROUTE: Lane.API,
    Layer.HANDLER: Lane.API,
    Layer.SERVICE: Lane.SVC,
    Layer.WORKER: Lane.SVC,
    Layer.DB: Lane.DB,
    Layer.CONFIG: Lane.OTHER,
    Layer.TEST: Lane.OTHER,
    Layer.DOCS: Lane.OTHER,
}


Source = Literal["rules", "llm", "cache", "fallback"]


@dataclass(frozen=True, slots=True)
class Route:
    query: str
    query_class: QueryClass
    source: Source  # who decided: rules alone, the model (now or cached), or rules after a failure
    literals: tuple[str, ...] = ()  # verbatim text from the query, kept in every sub-query
    identifiers: tuple[str, ...] = ()  # names in the query, then the model's guesses
    routes: tuple[str, ...] = ()  # /routes in the query, then the model's guesses
    paths: tuple[str, ...] = ()  # path filters: directories or files the query names
    layers: tuple[Layer, ...] = ()
    excluded_layers: tuple[Layer, ...] = ()  # "(not tests)", "not docs or tests"
    regex: str | None = None  # the pattern of a regex query
    symbol: str | None = None  # the name of a symbol query
    structural_rewrite: str | None = None
    behavioural_rewrite: str | None = None


# ---------------------------------------------------------------- rules

_BACKTICKED = re.compile(r"`([^`]+)`")
# A quote of the sentence opens after a space (or a bracket of the sentence) and closes before
# one, or before punctuation. A quote inside a token is code and leaves the token whole:
# `os.environ["SECRET_KEY"]`, `startswith("pk_live`.
_OPENS = r"(?:^|(?<=\s)|(?<=^\()|(?<=\s\())"
_CLOSES = r"(?=$|[\s.,;:!?)])"
# "double quotes", or a word opening a quote it never closes: `uses of "pk_live in the ...`.
_DOUBLE_QUOTED = re.compile(rf'{_OPENS}"([^"\s][^"]*?)"{_CLOSES}|{_OPENS}"([^"\s]+)(?=\s|$)')
# 'single quotes' (so "flow's" is not a quote).
_SINGLE_QUOTED = re.compile(rf"{_OPENS}'([^'\s][^']*?)'{_CLOSES}")
# Strings quoted inside a code token: `os.environ["SECRET_KEY"]` names SECRET_KEY.
_INNER_QUOTED = re.compile(r"""["']([^"'\s]+)["']""")
# Brackets are a pattern only as a character class: a range or a negation not attached to a
# name, or a quantifier after them. `Optional[User]` and `items[0]` are code, not patterns.
# A class is looked for within :data:`MAX_CLASS_CHARS` of its bracket, and a range at its first
# dash: the search stays linear in the token (unbounded, 40,000 brackets took seconds).
MAX_CLASS_CHARS = 64
_REGEX_SIGNS = re.compile(
    r"\\[\\()\[\]{}.*+?|^$dwsbDWSB]|\.\*|\.\+|\(\?|\|\w"
    rf"|(?<![\w\])])\[(?:\^|[^\]\-]{{0,{MAX_CLASS_CHARS}}}\w-\w)[^\]]{{0,{MAX_CLASS_CHARS}}}\]"
    rf"|\[[^\]]{{1,{MAX_CLASS_CHARS}}}\][*+?{{]"
)
# A pattern written as in JavaScript or an editor, between slashes with optional flags:
# /SECRET_\w+/, /todo\(.*\)/i. Only the inside is searched for (as (?i)… for the flags).
_DELIMITED = re.compile(r"^/(.+)/([im]{0,2})$", re.DOTALL)
_CODE_SIGNS = re.compile(r"""[()=<>{}\[\]"';]|::|->|=>""")
_CONSTANT = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_ROUTE = re.compile(r"^/[\w{}:.\-]+(?:/[\w{}:.\-]*)*$")
_FILE = re.compile(
    r"^(?:[\w.\-]+/)*[\w\-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|json|ya?ml|toml|md|sql|ini|cfg"
    r"|html|css|scss|sh|txt|lock|env)$"
)
_DIRECTORY = re.compile(r"^(?:[\w.\-]+/)+[\w.\-]*$")
# A directory named in prose looks like one: a trailing slash, or a segment with ".", "_" or "-"
# (``app/api/``, ``src/my_app``, ``.github/workflows``). ``client/server``, ``and/or``, ``i/o``
# and ``10/25/2026`` aren't paths.
_PATH_SIGN = re.compile(r"/$|[._\-]")
_CAMEL = re.compile(r"^[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+$")  # useFlowStore
_PASCAL_COMPOUND = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+$")  # JobQueueService
_SNAKE = re.compile(r"^_*[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")  # build_custom_component_template
_DOTTED = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")  # CompanyService.search
_WORD = re.compile(r"^[A-Za-z_$][\w$]*$")
_TRIM = "?!.,:;"
_CONTRACTION = re.compile(r"^[A-Za-z]+['\u2019][A-Za-z]{1,3}$")
_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc", "vs", "cf", "viz", "a.k.a"})  # trailing dot cut

# "where is the X class defined", "go to X", "X function": a definition asked for by name.
_DEFINITION_WORDS = frozenset(
    {
        "where",
        "is",
        "are",
        "the",
        "a",
        "an",
        "of",
        "find",
        "show",
        "me",
        "go",
        "jump",
        "to",
        "open",
        "locate",
        "definition",
        "defined",
        "declared",
        "implemented",
        "located",
        "lives",
        "live",
        "does",
        "do",
        # "where is X defined in api/users.py (or under src/)": the path narrows the search.
        "in",
        "inside",
        "under",
        "within",
        "or",
        "and",
    }
)
_KIND_WORDS = frozenset(
    {
        "class",
        "function",
        "method",
        "hook",
        "component",
        "type",
        "interface",
        "enum",
        "variable",
        "constant",
        "module",
        "struct",
        "decorator",
        "def",
        "model",
        "schema",
        "table",
        "view",
        "store",
        "dataclass",
        "protocol",
        "mixin",
        "field",
        "property",
        "attribute",
        "prop",
        "props",
    }
)
# Words that ask how or why, so a quoted string in the question is context, not the target.
_EXPLAIN = re.compile(
    r"\b(how|why|explain|describe|walk me through|what happens|what does|what do|what is the "
    r"(?:purpose|role|reason)|trace|what (?:would )?breaks?|should|could)\b",
    re.IGNORECASE,
)
_EXCLUDED = re.compile(
    r"\b(?:not|excluding|except|outside(?: of)?|without|no)\s+((?:docs?|tests?|documentation)"
    r"(?:\s*(?:,|or|and|/)\s*(?:docs?|tests?|documentation))*)",
    re.IGNORECASE,
)
_IMPACT = re.compile(
    r"\b(what (?:would |will )?breaks?|impact of|blast radius|if (?:i|we|you) (?:change|rename|"
    r"remove|delete|drop)|what depends on|who (?:calls|uses)|what calls|which .{0,40}"
    r"\b(?:depends? on|calls?|uses?|breaks?)\b|affected (?:by|if)|"
    r"safe to (?:change|remove|delete|rename))",
    re.IGNORECASE,
)
_ARCHITECTURE = re.compile(
    r"\b(architecture|high[- ]level|overview|how is .{0,60}(?:structured|organi[sz]ed|laid out"
    r"|split|designed)|structure of|(?:main|major) (?:parts|pieces|components|modules|layers)|"
    r"explain the .{0,40}(?:module|package|subsystem|system|layer|service)|what are the "
    r"(?:parts|layers|modules)|how .{0,30}fit together)\b",
    re.IGNORECASE,
)
_FLOW = re.compile(
    r"\b(trace|end[- ]to[- ]end|what happens (?:between|after|when)|from the .{1,60} to the|"
    r"which (?:endpoint|request|api|route|handler)|what request|request (?:goes out|is sent|"
    r"carries)|goes out|down to the (?:table|database|db)|ends up in the (?:database|db|table)|"
    r"what ends up|(?:is|gets) (?:saved|stored|written) (?:to|in) the (?:database|db)|"
    r"after (?:someone|a user|the user)|when (?:someone|a user|the user) .{0,80}"
    r"(?:what|which|where))",
    re.IGNORECASE,
)
_LAYER_WORDS: tuple[tuple[re.Pattern[str], Layer], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), layer)
    for pattern, layer in (
        (
            r"\b(ui|frontend|front-end|page|screen|dialog|modal|button|form|component|canvas|"
            r"sidebar|side bar|panel|editor|view)s?\b",
            Layer.UI,
        ),
        (r"\b(hook|store|state|cache[sd]?)\b", Layer.STATE),
        (r"\b(api client|fetch|axios|request goes out|client)\b", Layer.CLIENT),
        (r"\b(route|endpoint|url|path)s?\b", Layer.ROUTE),
        (r"\b(handler|controller|server|backend|back-end)s?\b", Layer.HANDLER),
        (r"\b(service|logic)s?\b", Layer.SERVICE),
        (
            r"\b(database|db|table|row|column|migration|schema|model|sql|query|queries)s?\b",
            Layer.DB,
        ),
        (r"\b(worker|background|job|queue|task|celery|cron)s?\b", Layer.WORKER),
        (r"\b(config|configuration|setting|env|environment variable|deploy)s?\b", Layer.CONFIG),
    )
)


@dataclass(slots=True)
class _Found:
    literals: list[str] = field(default_factory=list[str])
    quoted: list[str] = field(default_factory=list[str])  # "double" or 'single' quoted strings
    identifiers: list[str] = field(default_factory=list[str])
    routes: list[str] = field(default_factory=list[str])
    paths: list[str] = field(default_factory=list[str])
    regex: str | None = None


def _add(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _path(token: str) -> bool:
    """A file (a known extension) or a directory that looks like one (:data:`_PATH_SIGN`)."""
    if _FILE.match(token):
        return True
    return _DIRECTORY.match(token) is not None and _PATH_SIGN.search(token) is not None


def code_name(token: str) -> bool:
    """Whether ``token`` names something in code (camelCase, snake_case, CONSTANT_CASE, ...): a
    name worth searching for on its own, unlike ``a``, ``id`` or ``done``."""
    return _identifier(token) or _CONSTANT.match(token) is not None


def _identifier(token: str, *, plain_words: bool = False) -> bool:
    """A code name: camelCase, CompoundPascal, snake_case, dotted, CONSTANT_CASE; with
    ``plain_words``, any single word that could name something (``Graph``, ``Vertex``)."""
    if any(p.match(token) for p in (_CAMEL, _PASCAL_COMPOUND, _SNAKE, _DOTTED, _CONSTANT)):
        return True
    return plain_words and bool(_WORD.match(token))


def _pattern(token: str) -> str:
    """``token`` as a pattern: its ``/…/`` delimiters dropped when the inside is one."""
    match = _DELIMITED.match(token)
    if match is None or not _REGEX_SIGNS.search(match[1]):
        return token
    flags = "".join(sorted(set(match[2])))
    return f"(?{flags}){match[1]}" if flags else match[1]


def _scan_token(token: str, found: _Found) -> None:
    """Sort one whitespace-separated token (outside quotes) into what it is."""
    if _CONTRACTION.match(token) or token.lower() in _ABBREVIATIONS:  # editor's, e.g.
        return
    if _REGEX_SIGNS.search(token) and not _ROUTE.match(token):
        pattern = _pattern(token)
        _add(found.literals, pattern)
        found.regex = found.regex or pattern
    elif _ROUTE.match(token) and token.count("/") >= 1 and len(token) > 1:
        _add(found.literals, token)
        _add(found.routes, token)
    elif _path(token):
        _add(found.literals, token)
        _add(found.paths, token)
    elif _CODE_SIGNS.search(token):
        _add(found.literals, token)
        for inner in _INNER_QUOTED.findall(token):
            if _CONSTANT.match(inner) or _identifier(inner):
                _add(found.identifiers, inner)
    elif _CONSTANT.match(token) or _identifier(token):
        _add(found.literals, token)
        _add(found.identifiers, token)


def _scan(query: str) -> tuple[_Found, str]:
    """The literals, identifiers, routes and paths in ``query``; and the query with its quoted
    parts taken out (for the word rules)."""
    found = _Found()
    rest = query
    for pattern in (_BACKTICKED, _DOUBLE_QUOTED, _SINGLE_QUOTED):
        for match in pattern.finditer(rest):
            text = next(group for group in match.groups() if group)
            token = text.strip()
            regex = pattern is _BACKTICKED and _REGEX_SIGNS.search(token) is not None
            _add(found.literals, _pattern(token) if regex else text)
            if pattern is not _BACKTICKED:
                _add(found.quoted, text)
            if regex:
                found.regex = found.regex or _pattern(token)
            elif _ROUTE.match(token):
                _add(found.routes, token)
            elif _path(token):
                _add(found.paths, token)
            elif _identifier(token, plain_words=pattern is _BACKTICKED):
                _add(found.identifiers, token)
        rest = pattern.sub(" ", rest)
    for raw in rest.split():
        token = raw.strip(_TRIM.replace(".", "")).rstrip(".")  # .env, .github: a leading dot stays
        # Brackets of the sentence, not of code: "(not tests)", "(see above".
        if token.startswith("(") and ")" not in token:
            token = token[1:]
        if token.endswith(")") and "(" not in token:
            token = token[:-1]
        if token.startswith("(") and token.endswith(")"):
            token = token[1:-1]
        if token:
            _scan_token(token, found)
    return found, rest


def _definition_name(query: str, found: _Found) -> str | None:
    """The one name a "where is X defined" query asks for, if that is all it asks."""
    words = [w.strip(_TRIM).strip("`") for w in query.replace("`", " ` ").split()]
    words = [w for w in words if w and w != "`" and w not in found.paths]
    kind = any(w.lower() in _KIND_WORDS for w in words)
    names = [
        w for w in words if w.lower() not in _DEFINITION_WORDS and w.lower() not in _KIND_WORDS
    ]
    if len(names) != 1:
        return None
    [name] = names
    backticked = name in found.identifiers and f"`{name}`" in query
    asks_where = bool(re.match(r"^\s*(where|find|show|go to|jump to|open|locate)\b", query, re.I))
    capitalized = bool(_WORD.match(name)) and name[:1].isupper()
    if _identifier(name) or backticked or (capitalized and (kind or asks_where)):
        return name
    return None


def _excluded(query: str) -> tuple[Layer, ...]:
    layers: list[Layer] = []
    for match in _EXCLUDED.finditer(query):
        for word in re.findall(r"docs?|tests?|documentation", match.group(1), re.IGNORECASE):
            layer = Layer.TEST if word.lower().startswith("test") else Layer.DOCS
            if layer not in layers:
                layers.append(layer)
    return tuple(layers)


def _layers(text: str, excluded: Iterable[Layer]) -> tuple[Layer, ...]:
    skip = set(excluded)
    return tuple(
        layer for pattern, layer in _LAYER_WORDS if pattern.search(text) and layer not in skip
    )


_SERVER_SIDE = frozenset({Layer.ROUTE, Layer.HANDLER, Layer.DB})


def _guess_class(text: str, layers: tuple[Layer, ...]) -> QueryClass:
    """A class from cue words, for when the model can't be asked: a question that names both
    the UI and the server or database follows a flow."""
    if _IMPACT.search(text):
        return QueryClass.IMPACT
    if _ARCHITECTURE.search(text):
        return QueryClass.ARCHITECTURE
    if _FLOW.search(text) or (Layer.UI in layers and _SERVER_SIDE.intersection(layers)):
        return QueryClass.FLOW
    return QueryClass.CONCEPTUAL


@dataclass(frozen=True, slots=True)
class Classified:
    """What the rules make of a query: ``route`` (``source`` is ``"rules"`` when the rules
    decided its class; ``"fallback"`` when the class is only a guess) and whether the model
    is worth asking."""

    route: Route
    decided: bool


def classify(query: str) -> Classified:
    """Read ``query`` with rules (pure and deterministic): its first :data:`MAX_RULE_CHARS`. A
    longer query (a pasted log, then a question) is never decided by them: the question may be
    past what they read."""
    text = " ".join(query.split())
    # Cut at a space: a token cut in two would be a bogus literal or path.
    head = text if len(text) <= MAX_RULE_CHARS else text[:MAX_RULE_CHARS].rsplit(" ", 1)[0]
    found, unquoted = _scan(head)
    excluded = _excluded(head)
    base = Route(
        query=text,
        query_class=QueryClass.CONCEPTUAL,
        source="fallback",
        literals=tuple(found.literals),
        identifiers=tuple(found.identifiers),
        routes=tuple(found.routes),
        paths=tuple(found.paths),
        layers=_layers(unquoted, excluded),
        excluded_layers=excluded,
        regex=found.regex,
    )
    if len(head) < len(text):
        return Classified(replace(base, query_class=_guess_class(unquoted, base.layers)), False)
    if found.regex is not None and not _EXPLAIN.search(unquoted):
        return Classified(replace(base, query_class=QueryClass.REGEX, source="rules"), True)
    name = _definition_name(head, found)
    if name is not None:
        route = replace(
            base,
            query_class=QueryClass.SYMBOL,
            source="rules",
            symbol=name,
            literals=tuple(dict.fromkeys([*found.literals, name])),
            identifiers=tuple(dict.fromkeys([name, *found.identifiers])),
            layers=(),
        )
        return Classified(route, True)
    # A literal to find, with nothing asking how or why: "every use of "x" in the backend". A
    # path the query names narrows the search ("rate limiting in middleware/"), unless quoted.
    findable = [
        literal
        for literal in found.literals
        if literal in found.quoted
        or (literal not in found.identifiers and literal not in found.paths)
        or _CONSTANT.match(literal)
    ]
    if findable and not _EXPLAIN.search(unquoted) and not _FLOW.search(unquoted):
        return Classified(replace(base, query_class=QueryClass.REGEX, source="rules"), True)
    return Classified(replace(base, query_class=_guess_class(unquoted, base.layers)), False)


# ---------------------------------------------------------------- the model


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query_class: QueryClass = Field(alias="class")
    layers: list[Layer] = Field(default_factory=list[Layer])
    literals: list[str] = Field(default_factory=list[str])
    identifiers_guess: list[str] = Field(default_factory=list[str])
    routes_guess: list[str] = Field(default_factory=list[str])
    structural_rewrite: str = ""
    behavioural_rewrite: str = ""


def answer_schema() -> dict[str, Any]:
    """The JSON schema the model answers in."""
    strings: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "class": {"type": "string", "enum": [c.value for c in QueryClass]},
            "layers": {
                "type": "array",
                "items": {"type": "string", "enum": [layer.value for layer in Layer]},
            },
            "literals": strings,
            "identifiers_guess": strings,
            "routes_guess": strings,
            "structural_rewrite": {"type": "string"},
            "behavioural_rewrite": {"type": "string"},
        },
        "required": [
            "class",
            "layers",
            "literals",
            "identifiers_guess",
            "routes_guess",
            "structural_rewrite",
            "behavioural_rewrite",
        ],
        "additionalProperties": False,
    }


@cache
def system_prompt() -> str:
    path = resources.files("sensei_engine.search").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


def normalize(query: str) -> str:
    """The cache's view of a query: Unicode-normalized, case-folded, whitespace collapsed, a
    trailing question mark or full stop dropped."""
    text = unicodedata.normalize("NFKC", query).casefold()
    return " ".join(text.split()).rstrip(" ?.!")


def cache_key(query: str, model: str, scope: str) -> str:
    """The cache entry for ``query`` within ``scope`` (an org id)."""
    if not scope:
        msg = "a cache scope is required"
        raise ValueError(msg)
    parts = f"{PROMPT_VERSION}\0{model}\0{scope}\0{normalize(query)}"
    return CACHE_PREFIX + hashlib.sha256(parts.encode()).hexdigest()


def _clean(values: Iterable[str]) -> list[str]:
    return [v.strip() for v in values if v.strip()][:MAX_GUESSES]


def _verbatim(literal: str, query: str) -> bool:
    """Whether ``literal`` is in ``query`` as it stands, not inside a longer word (``port`` is
    not in "report")."""
    return re.search(rf"(?<!\w){re.escape(literal)}(?!\w)", query) is not None


def _merge(rules: Route, answer: _Answer, source: Source) -> Route:
    """The model's answer on top of the rules': the rules' literals and names come first and
    always stay; the model's literals count only if they are in the query verbatim, as whole
    words (:func:`_verbatim`)."""
    query = rules.query
    literals = [*rules.literals, *(lit for lit in _clean(answer.literals) if _verbatim(lit, query))]
    excluded = set(rules.excluded_layers)
    layers = [layer for layer in answer.layers if layer not in excluded] or list(rules.layers)
    return replace(
        rules,
        query_class=answer.query_class,
        source=source,
        literals=tuple(dict.fromkeys(literals)),
        identifiers=tuple(dict.fromkeys([*rules.identifiers, *_clean(answer.identifiers_guess)])),
        routes=tuple(dict.fromkeys([*rules.routes, *_clean(answer.routes_guess)])),
        layers=tuple(dict.fromkeys(layers)),
        structural_rewrite=answer.structural_rewrite.strip() or None,
        behavioural_rewrite=answer.behavioural_rewrite.strip() or None,
    )


class QueryRouter:
    """Routes queries: rules first, the model for what they can't decide, answers cached.

    ``redis`` may be None (no cache); ``client`` may be unconfigured (rules only)."""

    def __init__(
        self,
        client: AnthropicClient,
        redis: Redis | None = None,
        *,
        timeout_seconds: float = TIMEOUT_SECONDS,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._redis = redis
        self._timeout = timeout_seconds
        self._ttl = cache_ttl_seconds

    async def route(self, query: str, *, scope: str, on_usage: LLMUsageSink | None = None) -> Route:
        """Route ``query`` for ``scope`` (the org asking: answers are cached per org)."""
        classified = classify(query)
        if classified.decided or not self._client.configured:
            return classified.route
        rules = classified.route
        tier = tier_for(Role.ROUTER)
        model = self._client.model_for(tier)
        key = cache_key(rules.query, model, scope)
        cached = await self._cached(key)
        if cached is not None:
            return _merge(rules, cached, "cache")
        request = LLMRequest(
            rules.query,
            system=system_prompt(),
            tier=tier,
            max_tokens=MAX_TOKENS,
            effort="low",
            schema=answer_schema(),
            cache_system=True,
        )
        try:
            result = await self._client.complete_within(
                request, timeout_seconds=self._timeout, on_usage=on_usage
            )
            answer = _Answer.model_validate(result.data)
        except (LLMError, TimeoutError, ValidationError) as exc:
            log.warning("query router fell back to rules", error=type(exc).__name__)
            return rules
        await self._store(key, answer)
        return _merge(rules, answer, "llm")

    async def _cached(self, key: str) -> _Answer | None:
        if self._redis is None:
            return None
        try:
            raw: str | bytes | None = await self._redis.get(key)
        except RedisError as exc:
            log.warning("query router cache unavailable", error=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            return _Answer.model_validate_json(raw)
        except ValidationError:
            return None

    async def _store(self, key: str, answer: _Answer) -> None:
        if self._redis is None:
            return
        payload = json.dumps(answer.model_dump(mode="json", by_alias=True))
        try:
            await self._redis.set(key, payload, ex=self._ttl)
        except RedisError as exc:
            log.warning("query router cache unavailable", error=type(exc).__name__)
