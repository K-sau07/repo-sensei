"""Search plans: the original query's legs always, rewrites and guesses added, literals in every
sub-query, short-circuits without a model call, and the rules-only plan when the model fails."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import httpx2

from sensei_engine.search import plan as plans
from sensei_engine.search.plan import (
    REWRITE_LEGS,
    TEXT_LEGS,
    Leg,
    Mode,
    Origin,
    SearchPlan,
    SubQuery,
    plan,
    with_literals,
)
from sensei_engine.search.route import Layer, QueryClass, QueryRouter, Route, classify
from sensei_testing.anthropic import FakeAnthropic, message

ORG = "org-1"
QUERY = (
    'how does `InvoiceForm` post to /api/v2/invoices and where is "Card declined" raised '
    "(not tests)?"
)
ANSWER = {
    "class": "flow",
    "layers": ["ui", "handler", "db"],
    "literals": [],
    "identifiers_guess": ["create_invoice", "InvoiceForm"],
    "routes_guess": ["/invoices"],
    # Neither rewrite repeats the literals: the plan puts them back.
    "structural_rewrite": "invoice form component, invoices POST handler, invoices table",
    "behavioural_rewrite": "submit an invoice and save it, refuse a declined card",
}


def _api(answer: dict[str, object] | None = None) -> FakeAnthropic:
    reply = message(json.dumps(answer or ANSWER), model="claude-haiku-5-5")
    return FakeAnthropic({"/v1/messages": httpx2.Response(200, json=reply)})


async def _routed(query: str = QUERY, api: FakeAnthropic | None = None) -> Route:
    return await QueryRouter((api or _api()).client()).route(query, scope=ORG)


def _origins(found: SearchPlan) -> set[Origin]:
    return {sub.origin for sub in found.legs}


def _worded(sub: SubQuery) -> bool:
    return sub.leg in TEXT_LEGS and sub.origin in (
        Origin.ORIGINAL,
        Origin.STRUCTURAL,
        Origin.BEHAVIOURAL,
    )


async def test_every_sub_query_keeps_the_literals() -> None:
    route = await _routed()
    assert route.literals == ("InvoiceForm", "Card declined", "/api/v2/invoices")
    found = plan(route)
    assert found.mode is Mode.HYBRID
    assert {sub.literals for sub in found.legs} == {route.literals}
    worded = [sub for sub in found.legs if _worded(sub)]
    assert len(worded) == len(TEXT_LEGS) + 2 * len(REWRITE_LEGS)
    for sub in worded:
        assert all(literal in sub.text for literal in route.literals), sub


async def test_the_original_runs_whole_and_rewrites_add_legs() -> None:
    route = await _routed()
    found = plan(route)
    original = [(s.leg, s.text) for s in found.legs if s.origin is Origin.ORIGINAL]
    # Every text leg on the query as asked, before anything else.
    assert [(s.leg, s.text) for s in found.legs[: len(TEXT_LEGS)]] == [
        (leg, route.query) for leg in TEXT_LEGS
    ]
    assert (Leg.SYM, "InvoiceForm") in original  # the name in the query
    assert (Leg.GREP, "Card declined") in original  # its literals, as fixed strings
    for origin, rewrite in (
        (Origin.STRUCTURAL, route.structural_rewrite),
        (Origin.BEHAVIOURAL, route.behavioural_rewrite),
    ):
        assert rewrite is not None
        legs = [s for s in found.legs if s.origin is origin]
        assert [s.leg for s in legs] == list(REWRITE_LEGS)
        assert all(s.text.startswith(rewrite) for s in legs)


async def test_guesses_go_to_the_sym_path_and_grep_legs() -> None:
    found = plan(await _routed())
    by_origin = {(s.origin, s.leg, s.text) for s in found.legs}
    assert (Origin.IDENTIFIER, Leg.SYM, "create_invoice") in by_origin
    # A guess that is also in the query runs once, as the original's.
    assert [s.origin for s in found.of(Leg.SYM) if s.text == "InvoiceForm"] == [Origin.ORIGINAL]
    for route in ("/api/v2/invoices", "/invoices"):
        assert (Origin.ROUTE, Leg.LEX_PATH, route) in by_origin
        assert (Origin.ROUTE, Leg.GREP, route) in by_origin
    assert found.layers == (Layer.UI, Layer.HANDLER, Layer.DB)
    assert found.excluded_layers == (Layer.TEST,)


def test_paths_the_query_names_are_filters_and_path_and_grep_legs() -> None:
    route = classify("how is the session read in web/src/lib/session.ts?").route
    found = plan(route)
    assert found.paths == ("web/src/lib/session.ts",)
    legs = {(s.origin, s.leg) for s in found.legs if s.text == "web/src/lib/session.ts"}
    assert legs == {(Origin.PATH, Leg.LEX_PATH), (Origin.PATH, Leg.GREP)}
    # A path isn't grepped again as a literal, nor appended to its own leg's text.
    assert not [s for s in found.of(Leg.GREP) if s.origin is Origin.ORIGINAL]


async def test_a_symbol_query_goes_straight_to_find_symbol() -> None:
    api = _api()
    route = await _routed("where is the `PaymentQueueService` class defined?", api)
    found = plan(route)
    assert found.mode is Mode.SYMBOL
    assert found.legs == (
        SubQuery(Leg.SYM, "PaymentQueueService", Origin.ORIGINAL, ("PaymentQueueService",)),
    )
    assert api.requests == []  # no model call


async def test_a_pattern_or_a_literal_goes_straight_to_grep() -> None:
    api = _api()
    found = plan(await _routed("def test_.*_refund", api))
    assert found.mode is Mode.GREP
    assert [(s.leg, s.text, s.regex) for s in found.legs] == [(Leg.GREP, "test_.*_refund", True)]
    found = plan(await _routed('every use of "session_id_v2" or SESSION_TTL_SECONDS', api))
    assert found.mode is Mode.GREP
    assert [(s.text, s.regex) for s in found.legs] == [
        ("session_id_v2", False),
        ("SESSION_TTL_SECONDS", False),
    ]
    assert api.requests == []


def test_a_regex_route_with_only_a_path_is_searched_as_usual() -> None:
    route = replace(classify("app/models.py").route, query_class=QueryClass.REGEX)
    assert route.literals == route.paths == ("app/models.py",)
    assert plan(route).mode is Mode.HYBRID


async def test_when_the_model_fails_the_plan_is_the_rules_alone() -> None:
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    api = FakeAnthropic({"/v1/messages": httpx2.Response(529, json=overloaded)})
    route = await _routed(api=api)
    assert route.source == "fallback"
    found = plan(route)
    assert found.mode is Mode.HYBRID
    assert _origins(found) == {Origin.ORIGINAL, Origin.ROUTE}
    assert [(s.leg, s.text) for s in found.legs[: len(TEXT_LEGS)]] == [
        (leg, route.query) for leg in TEXT_LEGS
    ]
    assert {sub.literals for sub in found.legs} == {route.literals}
    assert found.of(Leg.SYM) == [SubQuery(Leg.SYM, "InvoiceForm", Origin.ORIGINAL, route.literals)]


def test_a_query_without_names_runs_sym_on_the_query() -> None:
    route = classify("how are refunds approved?").route
    assert [s.text for s in plan(route).of(Leg.SYM)] == [route.query]


def test_legs_are_not_repeated_and_are_capped() -> None:
    route = replace(
        classify("how do refunds work?").route,
        structural_rewrite="how do refunds work?",  # the same as the query: nothing new
        identifiers=tuple(f"name_{i}" for i in range(40)),
    )
    found = plan(route)
    keys = [(s.leg, s.text, s.regex) for s in found.legs]
    assert len(keys) == len(set(keys))
    assert Origin.STRUCTURAL not in _origins(found)
    assert len(found.legs) == plans.MAX_LEGS
    # The original's legs come first, so the cap takes guesses.
    assert found.legs[0].origin is Origin.ORIGINAL


def test_with_literals_appends_only_what_is_missing() -> None:
    assert with_literals("save the invoice", ["invoice", "/api/x"]) == "save the invoice /api/x"
    assert with_literals("x", []) == "x"
    # A literal inside a longer word isn't there.
    assert with_literals("write the report", ["port"]) == "write the report port"
    # One that starts or ends with punctuation is where it stands.
    assert with_literals("read app/.env with -v", [".env", "-v"]) == "read app/.env with -v"
    assert with_literals("pass -verbose", ["-v"]) == "pass -verbose -v"


def test_with_literals_takes_linear_time_on_a_long_query() -> None:
    text = "a" * 800_000
    started = time.perf_counter()
    for _ in range(5):
        with_literals(text, ["aaab", "b"])
    assert time.perf_counter() - started < 0.5


async def test_a_path_narrows_a_search_and_keeps_its_semantic_and_symbol_legs() -> None:
    api = _api()
    found = plan(await _routed("rate limiting in middleware/ for UserService", api))
    assert found.mode is Mode.HYBRID
    assert found.paths == ("middleware/",)
    assert {Leg.SEM_CODE, Leg.LEX_CODE, Leg.SYM} <= {sub.leg for sub in found.legs}
    assert found.of(Leg.SYM)[0].text == "UserService"
    # A definition asked for in a file: find_symbols, within the file.
    found = plan(await _routed("where is get_user defined in api/users.py?", api))
    assert found.mode is Mode.SYMBOL
    # Until the retriever filters by path, the path is a leg of its own.
    assert [(sub.leg, sub.text) for sub in found.legs] == [
        (Leg.SYM, "get_user"),
        (Leg.LEX_PATH, "api/users.py"),
    ]
    assert found.paths == ("api/users.py",)


async def test_grep_finds_a_quoted_name_however_it_is_read() -> None:
    found = plan(await _routed('os.environ["SECRET_KEY"]'))
    assert found.mode is Mode.GREP
    assert [(s.text, s.regex) for s in found.legs] == [
        ('os.environ["SECRET_KEY"]', False),
        ("SECRET_KEY", False),  # os.getenv("SECRET_KEY"), os.environ.get("SECRET_KEY")
    ]


async def test_only_code_names_quoted_in_a_literal_are_grepped_and_grep_legs_are_capped() -> None:
    for query, legs in (
        ('find print("a")', ['print("a")']),  # "a" would match nearly every line
        ("logger.info(\"done\") and x['id']", ['logger.info("done")', "x['id']"]),
        ('t("save") or t("pk_live")', ['t("save")', 't("pk_live")', "pk_live"]),
    ):
        found = plan(classify(query).route)
        assert found.mode is Mode.GREP
        assert [s.text for s in found.of(Leg.GREP)] == legs, query
    route = classify('"a1" "b1" "c1" "d1" "e1" "f1"').route
    assert len(plan(route).of(Leg.GREP)) == plans.MAX_GREP_LEGS


async def test_a_pattern_quoted_in_code_is_also_grepped_as_text() -> None:
    found = plan(await _routed("`re.compile(r'[0-9]+')`"))
    assert found.mode is Mode.GREP
    assert [(s.text, s.regex) for s in found.legs] == [
        ("re.compile(r'[0-9]+')", True),
        ("re.compile(r'[0-9]+')", False),  # the pattern doesn't match its own source
    ]
    # A bare pattern is grepped as one.
    assert [s.regex for s in plan(await _routed("TODO\\(billing\\)")).legs] == [True]
