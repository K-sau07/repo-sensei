"""The query router: what the rules decide alone, what they keep from every query, and how the
model's answer is merged, cached, and replaced by the rules when the model fails."""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from sensei_engine.ingest.constellation import LAYERS, OTHER_LAYER
from sensei_engine.llm.anthropic_client import LLMUsage
from sensei_engine.search.route import (
    LANES,
    Lane,
    Layer,
    QueryClass,
    QueryRouter,
    answer_schema,
    cache_key,
    classify,
    normalize,
    system_prompt,
)
from sensei_testing.anthropic import FakeAnthropic, message

# ---------------------------------------------------------------- rules


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("useCartStore", "useCartStore"),
        ("create_invoice_draft", "create_invoice_draft"),
        ("Invoice class", "Invoice"),
        ("where is the Ledger class defined?", "Ledger"),
        ("where is `PaymentQueueService` implemented?", "PaymentQueueService"),
        ("where is the `OrderUpdateIn` model declared?", "OrderUpdateIn"),
        ("show me the BillingService class", "BillingService"),
        ("go to useDebounce", "useDebounce"),
        ("the InvoiceList component", "InvoiceList"),
        ("`Ledger.post_entry`", "Ledger.post_entry"),
    ],
)
def test_a_definition_asked_for_by_name_is_a_symbol_query(query: str, name: str) -> None:
    classified = classify(query)
    assert classified.decided
    assert classified.route.query_class is QueryClass.SYMBOL
    assert classified.route.symbol == name
    assert name in classified.route.literals


@pytest.mark.parametrize(
    ("query", "literal"),
    [
        (
            'every use of the "session_id_v2" cookie name in the backend (not tests)',
            "session_id_v2",
        ),
        ("STRIPE_WEBHOOK_SECRET in the source code (not docs or tests)", "STRIPE_WEBHOOK_SECRET"),
        ('startswith("pk_live in the payments code', 'startswith("pk_live'),
        ('localStorage "auth_token"', "auth_token"),
        ("TODO\\(billing\\)", "TODO\\(billing\\)"),
        ("/api/v2/invoices/{invoice_id}", "/api/v2/invoices/{invoice_id}"),
        ('where do we raise "Card declined"', "Card declined"),
        ("def test_.*_refund", "test_.*_refund"),
    ],
)
def test_a_literal_to_find_is_a_regex_query(query: str, literal: str) -> None:
    classified = classify(query)
    assert classified.decided
    assert classified.route.query_class is QueryClass.REGEX
    assert literal in classified.route.literals


@pytest.mark.parametrize(
    ("query", "guess"),
    [
        ("how are refunds approved before money goes back to the card?", QueryClass.CONCEPTUAL),
        ("where do we decide which customers see the new pricing page?", QueryClass.CONCEPTUAL),
        ("how does the editor's autosave decide when to save?", QueryClass.CONCEPTUAL),
        ("trace a refund from the button on the order page to the ledger row", QueryClass.FLOW),
        (
            "when the checkout form is submitted, what request goes out and what is stored?",
            QueryClass.FLOW,
        ),
        ("how does the invoices page get its rows from the server?", QueryClass.FLOW),
        ("how is the billing module structured?", QueryClass.ARCHITECTURE),
        ("give me an overview of the notification system", QueryClass.ARCHITECTURE),
        ("what breaks if I rename the `amount` column on invoices?", QueryClass.IMPACT),
        ("who calls `send_receipt`?", QueryClass.IMPACT),
        # "refuse" is not "use": no impact cue.
        ("when an account is closed, in which case does the server refuse?", QueryClass.CONCEPTUAL),
    ],
)
def test_other_questions_go_to_the_model_with_a_guess(query: str, guess: QueryClass) -> None:
    classified = classify(query)
    assert not classified.decided
    assert classified.route.source == "fallback"
    assert classified.route.query_class is guess


def test_quoted_text_in_a_how_question_is_kept_but_does_not_decide() -> None:
    classified = classify('how is the "Card declined" message shown to the customer?')
    assert not classified.decided
    assert classified.route.literals == ("Card declined",)


def test_literals_routes_paths_and_exclusions_are_collected() -> None:
    route = classify(
        "how does `InvoiceForm` post to /api/v2/invoices from web/src/routes/invoices.tsx "
        "(not tests or docs)?"
    ).route
    assert route.literals == (
        "InvoiceForm",
        "/api/v2/invoices",
        "web/src/routes/invoices.tsx",
    )
    assert route.identifiers == ("InvoiceForm",)
    assert route.routes == ("/api/v2/invoices",)
    assert route.paths == ("web/src/routes/invoices.tsx",)
    assert route.excluded_layers == (Layer.TEST, Layer.DOCS)


def test_contractions_and_plain_words_are_not_literals() -> None:
    route = classify("what's the editor's undo limit and why isn't it configurable?").route
    assert route.literals == ()
    assert route.identifiers == ()


@pytest.mark.parametrize(
    ("query", "literals", "identifiers"),
    [
        ('os.environ["SECRET_KEY"]', ('os.environ["SECRET_KEY"]',), ("SECRET_KEY",)),
        (
            "where is settings['DATABASE_URL'] read?",
            ("settings['DATABASE_URL']",),
            ("DATABASE_URL",),
        ),
        ('call startswith("pk_live") on it', ('startswith("pk_live")',), ("pk_live",)),
    ],
)
def test_a_quote_inside_a_code_token_keeps_the_token_whole(
    query: str, literals: tuple[str, ...], identifiers: tuple[str, ...]
) -> None:
    route = classify(query).route
    assert route.literals == literals
    assert route.identifiers == identifiers
    assert route.regex is None


@pytest.mark.parametrize(
    "query", ["Optional[User]", "where is Dict[str, Any] used", "items[0]", "items[i-1]"]
)
def test_a_subscript_or_generic_is_a_literal_not_a_pattern(query: str) -> None:
    route = classify(query).route
    assert route.regex is None
    assert route.query_class is QueryClass.REGEX  # a literal to find
    assert route.literals[0] in query


@pytest.mark.parametrize("pattern", ["[a-z]+_id", "[^/]+\\.py", "id[0-9]+", "[A-F]"])
def test_a_character_class_is_a_pattern(pattern: str) -> None:
    assert classify(pattern).route.regex == pattern


def test_abbreviations_are_not_literals() -> None:
    route = classify("how do caches expire, e.g. the session one, i.e. redis etc.?").route
    assert route.literals == ()
    assert route.identifiers == ()


def test_rules_are_deterministic() -> None:
    query = 'where do we read "X-Request-Id" in the api (not tests)?'
    assert classify(query) == classify(query)


# ---------------------------------------------------------------- the model

ANSWER = {
    "class": "flow",
    "layers": ["ui", "handler", "db", "test"],
    "literals": ["/api/v2/invoices", "made-up literal", "invoice", "voice"],
    "identifiers_guess": ["InvoiceForm", "create_invoice", "InvoiceCreate"],
    "routes_guess": ["/api/v2/invoices", "/invoices"],
    "structural_rewrite": "InvoiceForm component, POST /api/v2/invoices handler, invoices table",
    "behavioural_rewrite": "submit the invoice form and save a new invoice row",
}
ORG = "org-1"
QUERY = "how does the invoice form reach /api/v2/invoices and what row is written (not tests)?"


def _api(answer: dict[str, object] | None = None) -> FakeAnthropic:
    text = json.dumps(answer or ANSWER)
    reply = message(text, model="claude-haiku-5-5")
    return FakeAnthropic({"/v1/messages": httpx2.Response(200, json=reply)})


async def test_the_model_routes_what_the_rules_cannot_and_literals_survive() -> None:
    api = _api()
    billed: list[LLMUsage] = []

    async def sink(usage: LLMUsage) -> None:
        billed.append(usage)

    route = await QueryRouter(api.client()).route(QUERY, on_usage=sink, scope=ORG)

    [body] = api.bodies("/v1/messages")
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["schema"] == answer_schema()
    assert body["system"][0]["text"] == system_prompt()
    assert body["messages"][0]["content"] == QUERY
    assert route.source == "llm"
    assert route.query_class is QueryClass.FLOW
    # The rules' literal stays; the model's are kept only if in the query as whole words:
    # "invoice" is, "voice" (inside "invoice") and the made-up one aren't.
    assert route.literals == ("/api/v2/invoices", "invoice")
    assert route.identifiers == ("InvoiceForm", "create_invoice", "InvoiceCreate")
    assert route.routes == ("/api/v2/invoices", "/invoices")
    assert route.layers == (Layer.UI, Layer.HANDLER, Layer.DB)  # "(not tests)" holds
    assert route.excluded_layers == (Layer.TEST,)
    assert route.structural_rewrite is not None
    assert route.behavioural_rewrite is not None
    assert len(billed) == 1


async def test_a_rule_decided_query_never_reaches_the_model() -> None:
    api = _api()
    route = await QueryRouter(api.client()).route(
        "where is `PaymentQueueService` defined?", scope=ORG
    )
    assert (route.source, route.query_class) == ("rules", QueryClass.SYMBOL)
    assert api.requests == []


async def test_without_a_key_the_rules_route_alone() -> None:
    api = _api()
    route = await QueryRouter(api.client(api_key=None)).route(QUERY, scope=ORG)
    assert (route.source, route.query_class) == ("fallback", QueryClass.FLOW)
    assert api.requests == []


async def test_answers_are_cached_per_normalized_query() -> None:
    api = _api()
    redis = FakeAsyncRedis(decode_responses=True)
    router = QueryRouter(api.client(), redis, cache_ttl_seconds=60)

    first = await router.route(QUERY, scope=ORG)
    again = await router.route("  " + QUERY.upper().replace("?", "") + " ", scope=ORG)

    assert (first.source, again.source) == ("llm", "cache")
    assert len(api.requests) == 1
    key = cache_key(QUERY, "claude-haiku-5-5", ORG)
    assert 0 < await redis.ttl(key) <= 60
    assert again.query_class is first.query_class
    # Literals come from the query asked, in its own spelling.
    assert again.literals == ("/API/V2/INVOICES",)


async def test_orgs_never_share_a_cached_answer() -> None:
    api = _api()
    redis = FakeAsyncRedis(decode_responses=True)
    router = QueryRouter(api.client(), redis, cache_ttl_seconds=60)

    mine = await router.route(QUERY, scope="org-a")
    theirs = await router.route(QUERY, scope="org-b")

    # The second org pays for its own answer: a cache hit would tell it the first one asked.
    assert (mine.source, theirs.source) == ("llm", "llm")
    assert len(api.requests) == 2
    assert cache_key(QUERY, "claude-haiku-5-5", "org-a") != cache_key(
        QUERY, "claude-haiku-5-5", "org-b"
    )
    with pytest.raises(ValueError, match="scope"):
        cache_key(QUERY, "claude-haiku-5-5", "")


def test_normalize() -> None:
    assert normalize("  Where IS   the Login form? ") == "where is the login form"
    assert normalize("\uff46\uff55\uff4c\uff4c width?") == "full width"  # full-width letters


async def test_a_broken_cache_is_skipped() -> None:
    class Broken(FakeAsyncRedis):
        async def get(self, *_: object, **__: object) -> None:  # type: ignore[override]
            raise RedisConnectionError

        async def set(self, *_: object, **__: object) -> None:  # type: ignore[override]
            raise RedisConnectionError

    api = _api()
    route = await QueryRouter(api.client(), Broken()).route(QUERY, scope=ORG)
    assert route.source == "llm"


@pytest.mark.parametrize(
    "reply",
    [
        message("", stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"}),
        message('{"class": "flow", "layers"', stop_reason="max_tokens"),
        message(json.dumps({**ANSWER, "class": "poetry"})),  # not a class
        message("not json at all"),
    ],
)
async def test_a_failed_answer_falls_back_to_the_rules(reply: dict[str, object]) -> None:
    api = FakeAnthropic({"/v1/messages": httpx2.Response(200, json=reply)})
    billed: list[LLMUsage] = []

    async def sink(usage: LLMUsage) -> None:
        billed.append(usage)

    route = await QueryRouter(api.client()).route(QUERY, on_usage=sink, scope=ORG)

    assert (route.source, route.query_class) == ("fallback", QueryClass.FLOW)
    assert route.literals == ("/api/v2/invoices",)
    assert len(billed) == 1  # billed all the same


async def test_api_errors_fall_back_to_the_rules() -> None:
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    api = FakeAnthropic({"/v1/messages": httpx2.Response(529, json=overloaded)})
    route = await QueryRouter(api.client()).route(QUERY, scope=ORG)
    assert route.source == "fallback"


async def test_a_slow_model_is_not_waited_for() -> None:
    async def slow(_: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json=message(json.dumps(ANSWER)))

    api = FakeAnthropic({"/v1/messages": slow})
    redis = FakeAsyncRedis(decode_responses=True)
    router = QueryRouter(api.client(), redis, timeout_seconds=0.05)
    billed: list[LLMUsage] = []

    async def sink(usage: LLMUsage) -> None:
        billed.append(usage)

    route = await router.route(QUERY, scope=ORG, on_usage=sink)

    assert route.source == "fallback"
    # It may still be billed: the ledger gets the most it can cost.
    [usage] = billed
    assert usage.output_tokens > 0
    assert (
        await redis.ttl(cache_key(QUERY, "claude-haiku-5-5", ORG)) == -2
    )  # a fallback isn't cached


def test_every_layer_folds_into_one_of_the_design_systems_lanes() -> None:
    assert set(LANES) == set(Layer)
    assert {LANES[layer] for layer in Layer} == set(Lane)
    assert [LANES[layer] for layer in (Layer.STATE, Layer.HANDLER, Layer.WORKER)] == [
        Lane.UI,
        Lane.API,
        Lane.SVC,
    ]
    # The same five names the constellation (and the API) use.
    assert {lane.value for lane in Lane} == {*LAYERS.values(), OTHER_LAYER}
