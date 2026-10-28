"""Search plans (2.10): a routed query (:mod:`sensei_engine.search.route`) turned into the legs
the retriever (2.9) runs and fuses. Pure: no model call, no database.

* **Short-circuits.** A ``symbol`` query is one ``sym`` leg, run by
  :func:`sensei_engine.symbols.lookup.find_symbols`. A ``regex`` query (a pattern, or a literal
  to find) is ``grep`` legs, run by :class:`sensei_engine.search.grep.ExactSearch`: the pattern
  (and, when it is quoted code like ``re.compile(r'[0-9]+')``, its text as a fixed string), or each
  literal as a fixed string, with the code names quoted inside it (``os.environ["SECRET_KEY"]``
  finds ``getenv("SECRET_KEY")`` too; ``print("a")`` adds no ``a``): at most
  :data:`MAX_GREP_LEGS`. The paths the query names narrow either one; until the retriever has
  path filters (2.10.1), each is also a ``lex_path`` leg.
* **Everything else is additive.** The original query always runs as its own full leg set
  (every text leg, and ``sym`` on the names it holds). The model's two rewrites each add the
  content legs, never replace the original's. Names the model guesses add ``sym`` legs; routes
  and the paths the query names add ``lex_path`` and ``grep`` legs; the query's literals add
  ``grep`` legs.
* **Literals are never lost.** Every sub-query carries all of the query's literals; the text of
  a text leg for the query or a rewrite holds each of them (appended when a rewrite left one
  out), so the retriever can match them however the leg searches. (A path, route or name leg
  searches for that alone.)

A route the rules settled after the model failed (``source="fallback"``) has no rewrites or
guesses: its plan is the original's legs and what the rules found.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sensei_engine.search.route import Layer, QueryClass, Route, code_name

MAX_LEGS = 32  # in plan order: the original's first, guesses last
MAX_GREP_LEGS = 4  # what one ExactSearch runs (sensei_engine.search.grep)


class Leg(enum.StrEnum):
    SEM_CODE = "sem_code"  # embeddings of code chunks
    SEM_SUMMARY = "sem_summary"  # embeddings of summary-view chunks
    LEX_CODE = "lex_code"  # BM25 over code
    LEX_PATH = "lex_path"  # BM25 over paths
    LEX_SUMMARY = "lex_summary"  # BM25 over summaries (weak)
    SYM = "sym"  # symbol names (trigram)
    GREP = "grep"  # exact text or a pattern (ripgrep)
    PPR = "ppr"  # the code graph around the other legs' best (3.6): no text of its own


# What searches text: the original query and each rewrite.
TEXT_LEGS = (Leg.SEM_CODE, Leg.SEM_SUMMARY, Leg.LEX_CODE, Leg.LEX_PATH, Leg.LEX_SUMMARY)
# A rewrite describes what the code does or which parts are involved: the content legs.
REWRITE_LEGS = (Leg.SEM_CODE, Leg.SEM_SUMMARY, Leg.LEX_CODE, Leg.LEX_SUMMARY)
_WORDED = frozenset({"original", "structural", "behavioural"})  # origins whose text is words


class Origin(enum.StrEnum):
    """Where a sub-query's text comes from."""

    ORIGINAL = "original"  # the query as asked, or a name or literal in it
    STRUCTURAL = "structural"  # the model's structural rewrite
    BEHAVIOURAL = "behavioural"  # the model's behavioural rewrite
    IDENTIFIER = "identifier"  # a name the model guessed
    ROUTE = "route"  # a route, in the query or guessed
    PATH = "path"  # a path the query names


class Mode(enum.StrEnum):
    HYBRID = "hybrid"  # every leg, fused (2.9)
    SYMBOL = "symbol"  # find_symbols only
    GREP = "grep"  # ripgrep only


@dataclass(frozen=True, slots=True)
class SubQuery:
    leg: Leg
    text: str  # what the leg searches for: words, a name, a path, a literal or a pattern
    origin: Origin
    literals: tuple[str, ...]  # the query's literals, kept by every sub-query
    regex: bool = False  # grep: ``text`` is a pattern (else a fixed string)


@dataclass(frozen=True, slots=True)
class SearchPlan:
    route: Route
    mode: Mode
    legs: tuple[SubQuery, ...]
    paths: tuple[str, ...] = ()  # the paths the query names (2.10.1's path filters)
    layers: tuple[Layer, ...] = ()
    excluded_layers: tuple[Layer, ...] = ()

    def of(self, leg: Leg) -> list[SubQuery]:
        return [sub for sub in self.legs if sub.leg is leg]


# A name quoted inside a code literal: `os.environ["SECRET_KEY"]`, `settings['DATABASE_URL']`.
_INNER_QUOTED = re.compile(r"""["']([A-Za-z_$][\w$.\-]*)["']""")
_QUOTES = frozenset("\"'")


def with_literals(text: str, literals: Iterable[str]) -> str:
    """``text`` with each literal it doesn't hold (as a whole word: ``port`` isn't in "report")
    appended."""
    missing = [literal for literal in literals if not _in_query(literal, text)]
    return " ".join([text, *missing]) if missing else text


def _word(char: str) -> bool:
    return char.isalnum() or char == "_"


def _in_query(value: str, text: str) -> bool:
    """Whether ``text`` holds ``value`` whole: not inside a longer word, on each side where
    ``value`` starts or ends with a word character (``port`` isn't in "report"; ``.env`` and
    ``-v`` are where they stand). Plain string search: linear in ``text``."""
    if not value:
        return False
    start = text.find(value)
    while start != -1:
        end = start + len(value)
        left = not _word(value[0]) or start == 0 or not _word(text[start - 1])
        right = not _word(value[-1]) or end == len(text) or not _word(text[end])
        if left and right:
            return True
        start = text.find(value, start + 1)
    return False


def plan(route: Route) -> SearchPlan:
    """The legs for ``route``; see the module."""
    if route.query_class is QueryClass.SYMBOL and route.symbol:
        legs = (SubQuery(Leg.SYM, route.symbol, Origin.ORIGINAL, route.literals),)
        return _plan(route, Mode.SYMBOL, legs + _paths(route))
    if route.query_class is QueryClass.REGEX:
        grep = _grep(route)
        if grep:
            return _plan(route, Mode.GREP, grep + _paths(route))
    return _plan(route, Mode.HYBRID, _hybrid(route))


def _paths(route: Route) -> tuple[SubQuery, ...]:
    """A ``lex_path`` leg per path the query names: a short-circuit's narrowing, until the
    retriever filters by path (2.10.1)."""
    return tuple(SubQuery(Leg.LEX_PATH, path, Origin.PATH, route.literals) for path in route.paths)


def _plan(route: Route, mode: Mode, legs: tuple[SubQuery, ...]) -> SearchPlan:
    return SearchPlan(route, mode, legs, route.paths, route.layers, route.excluded_layers)


def _grep(route: Route) -> tuple[SubQuery, ...]:
    """The pattern (and its text, if it holds quotes: code that quotes a pattern doesn't match
    itself), or each literal that isn't a path filter as a fixed string, then the names quoted
    inside them (whatever reads ``SECRET_KEY``, not one way of reading it)."""
    literals = route.literals
    if route.regex is not None:
        pattern = SubQuery(Leg.GREP, route.regex, Origin.ORIGINAL, literals, regex=True)
        if _QUOTES.isdisjoint(route.regex):
            return (pattern,)
        return (pattern, SubQuery(Leg.GREP, route.regex, Origin.ORIGINAL, literals))
    fixed = [literal for literal in literals if literal not in route.paths]
    # Only code names: print("a"), logger.info("done") or x['id'] would match nearly every line.
    inner = [
        name for literal in fixed for name in _INNER_QUOTED.findall(literal) if code_name(name)
    ]
    texts = list(dict.fromkeys(fixed + inner))[:MAX_GREP_LEGS]
    return tuple(SubQuery(Leg.GREP, text, Origin.ORIGINAL, literals) for text in texts)


def _hybrid(route: Route) -> tuple[SubQuery, ...]:
    query, literals = route.query, route.literals
    legs: list[SubQuery] = []
    seen: set[tuple[Leg, str, bool]] = set()

    def add(leg: Leg, text: str, origin: Origin, *, regex: bool = False) -> None:
        text = text.strip()
        if leg in TEXT_LEGS and origin.value in _WORDED:
            text = with_literals(text, literals)
        if text and (leg, text, regex) not in seen and len(legs) < MAX_LEGS:
            seen.add((leg, text, regex))
            legs.append(SubQuery(leg, text, origin, literals, regex))

    # The original, always, whole.
    for leg in TEXT_LEGS:
        add(leg, query, Origin.ORIGINAL)
    named = [name for name in route.identifiers if _in_query(name, query)]
    for name in named or [query]:
        add(Leg.SYM, name, Origin.ORIGINAL)
    if route.regex is not None:
        add(Leg.GREP, route.regex, Origin.ORIGINAL, regex=True)
    for literal in literals:
        if literal not in route.paths and literal not in route.routes:
            add(Leg.GREP, literal, Origin.ORIGINAL)
    # Rewrites add legs.
    for rewrite, origin in (
        (route.structural_rewrite, Origin.STRUCTURAL),
        (route.behavioural_rewrite, Origin.BEHAVIOURAL),
    ):
        if rewrite:
            for leg in REWRITE_LEGS:
                add(leg, rewrite, origin)
    # Routes and paths: by path, and as text.
    for value, origin in (
        *((r, Origin.ROUTE) for r in route.routes),
        *((p, Origin.PATH) for p in route.paths),
    ):
        add(Leg.LEX_PATH, value, origin)
        add(Leg.GREP, value, origin)
    # Names the model guessed.
    for name in route.identifiers:
        if name not in named:
            add(Leg.SYM, name, Origin.IDENTIFIER)
    return tuple(legs)
