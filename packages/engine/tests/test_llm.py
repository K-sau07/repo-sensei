"""The Anthropic client against a fake API: the request it sends, what it makes of each kind of
answer (JSON, refusal, cut off, fallback), its errors, token counts and batches; and batches
tracked in the database, collected once, their usage in the ledger."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import AnthropicSettings, Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import LLMBatchStatus, UsageEvent, UsageKind
from sensei_core.orgs.service import OrgService
from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator
from sensei_engine.llm import batches
from sensei_engine.llm.anthropic_client import (
    FALLBACK_BETA,
    AnthropicClient,
    LLMBadOutput,
    LLMError,
    LLMNotConfigured,
    LLMRefused,
    LLMRequest,
    LLMTruncated,
    LLMUnavailable,
    LLMUsage,
    Tier,
)
from sensei_engine.llm.ledger import usage_ledger
from sensei_testing import make_user

# ---------------------------------------------------------------- a fake Anthropic API

BASE_URL = "https://api.anthropic.test"


def settings(**overrides: Any) -> AnthropicSettings:
    values: dict[str, Any] = {
        "api_key": SecretStr("sk-ant-test"),
        "base_url": BASE_URL,
        "max_retries": 0,
        **overrides,
    }
    return AnthropicSettings(**values)


def message(
    text: str,
    *,
    model: str = "claude-sonnet-5-5",
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage
        or {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def batch(batch_id: str, status: str = "in_progress", **counts: int) -> dict[str, Any]:
    ended = status == "ended"
    return {
        "id": batch_id,
        "type": "message_batch",
        "processing_status": status,
        "request_counts": {
            "processing": 0 if ended else sum(counts.values()) or 1,
            "succeeded": counts.get("succeeded", 0),
            "errored": counts.get("errored", 0),
            "canceled": counts.get("canceled", 0),
            "expired": counts.get("expired", 0),
        },
        "created_at": "2026-10-24T00:00:00Z",
        "expires_at": "2026-10-25T00:00:00Z",
        "ended_at": "2026-10-24T00:30:00Z" if ended else None,
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": f"{BASE_URL}/v1/messages/batches/{batch_id}/results" if ended else None,
    }


Handler = Callable[[httpx2.Request], httpx2.Response]


@dataclass
class FakeAnthropic:
    """Answers each path from ``routes`` (a response, or a function of the request) and keeps
    every request it saw."""

    routes: dict[str, httpx2.Response | Handler] = field(
        default_factory=dict[str, httpx2.Response | Handler]
    )
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx2.Response(
                404,
                json={
                    "type": "error",
                    "error": {"type": "not_found_error", "message": request.url.path},
                },
            )
        return route(request) if callable(route) else route

    def client(self, **overrides: Any) -> AnthropicClient:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        return AnthropicClient(settings(**overrides), http_client=http)

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def jsonl(*lines: dict[str, Any]) -> httpx2.Response:
    content = "\n".join(json.dumps(line) for line in lines).encode()
    return httpx2.Response(200, content=content, headers={"content-type": "application/binary"})


# ---------------------------------------------------------------- the client

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


async def _collect(sink: list[LLMUsage], usage: LLMUsage) -> None:
    sink.append(usage)


async def test_a_request_names_the_tier_model_effort_schema_and_cached_system() -> None:
    api = FakeAnthropic({"/v1/messages": httpx2.Response(200, json=message('{"summary": "adds"}'))})
    billed: list[LLMUsage] = []
    request = LLMRequest(
        "def add(a, b): ...",
        system="Summarize.",
        tier=Tier.FAST,
        max_tokens=2000,
        effort="low",
        schema=SCHEMA,
        cache_system=True,
    )

    result = await api.client().complete(request, on_usage=lambda u: _collect(billed, u))

    [sent] = api.requests
    body = json.loads(sent.content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 2000
    assert body["messages"] == [{"role": "user", "content": "def add(a, b): ..."}]
    assert body["system"] == [
        {"type": "text", "text": "Summarize.", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    # A declined request is retried server-side on the recommended model.
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in sent.headers["anthropic-beta"]
    assert sent.headers["authorization"] == "Bearer sk-ant-test"
    assert result.data == {"summary": "adds"}
    assert billed == [LLMUsage("anthropic", "claude-sonnet-5-5", 100, 20)]
    assert result.usage == tuple(billed)


async def test_without_fallbacks_or_options_the_request_is_plain() -> None:
    api = FakeAnthropic({"/v1/messages": httpx2.Response(200, json=message("hello"))})

    result = await api.client(refusal_fallback=False).complete(LLMRequest("hi", tier=Tier.DEEP))

    [body] = api.bodies("/v1/messages")
    assert body["model"] == "claude-opus-5-5"
    assert not {"fallbacks", "system", "output_config"} & body.keys()
    assert "anthropic-beta" not in api.requests[0].headers
    assert (result.text, result.data) == ("hello", None)


async def test_a_fallback_bills_each_model_that_ran() -> None:
    usage = {
        "input_tokens": 300,
        "output_tokens": 50,
        "iterations": [
            {
                "type": "message",
                "model": "claude-sonnet-5-5",
                "input_tokens": 150,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            {
                "type": "fallback_message",
                "model": "claude-sonnet-5",
                "input_tokens": 150,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        ],
    }
    answer = message("ok", model="claude-sonnet-5", usage=usage)
    api = FakeAnthropic({"/v1/messages": httpx2.Response(200, json=answer)})

    result = await api.client().complete(LLMRequest("hi"))

    assert result.model == "claude-sonnet-5"
    assert result.usage == (
        LLMUsage("anthropic", "claude-sonnet-5-5", 150, 0),
        LLMUsage("anthropic", "claude-sonnet-5", 150, 50, 10, 5),
    )


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (
            message(
                "", stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"}
            ),
            LLMRefused,
        ),
        (message('{"summary": "cut', stop_reason="max_tokens"), LLMTruncated),
        (message("Sure! Here is the summary"), LLMBadOutput),
    ],
)
async def test_an_unusable_answer_raises_after_its_usage_is_billed(
    answer: dict[str, Any], error: type[LLMError]
) -> None:
    api = FakeAnthropic({"/v1/messages": httpx2.Response(200, json=answer)})
    billed: list[LLMUsage] = []

    with pytest.raises(error) as raised:
        await api.client().complete(
            LLMRequest("x", schema=SCHEMA), on_usage=lambda u: _collect(billed, u)
        )

    assert len(billed) == 1
    if isinstance(raised.value, LLMRefused):
        assert raised.value.category == "cyber"


async def test_api_and_network_failures_are_ours() -> None:
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    api = FakeAnthropic({"/v1/messages": httpx2.Response(529, json=overloaded)})
    with pytest.raises(LLMError) as raised:
        await api.client().complete(LLMRequest("x"))
    assert raised.value.status == 529

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    api = FakeAnthropic({"/v1/messages": unreachable})
    with pytest.raises(LLMUnavailable):
        await api.client().complete(LLMRequest("x"))


async def test_the_sdk_retries_overloads_before_giving_up() -> None:
    answers = iter(
        [
            httpx2.Response(
                529,
                json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
                headers={"retry-after-ms": "1"},
            ),
            httpx2.Response(200, json=message("ok")),
        ]
    )
    api = FakeAnthropic({"/v1/messages": lambda _: next(answers)})

    result = await api.client(max_retries=1).complete(LLMRequest("x"))

    assert result.text == "ok"
    assert len(api.requests) == 2


async def test_no_key_means_no_request() -> None:
    api = FakeAnthropic()
    client = api.client(api_key=None)
    assert not client.configured
    with pytest.raises(LLMNotConfigured):
        await client.complete(LLMRequest("x"))
    with pytest.raises(LLMNotConfigured):
        await client.count_tokens(LLMRequest("x"))
    assert api.requests == []


def test_tests_never_use_a_developers_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # sensei_testing strips SENSEI_ANTHROPIC_* and the .env file for every test.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert Settings(env="test").anthropic.api_key is None


async def test_count_tokens() -> None:
    api = FakeAnthropic(
        {"/v1/messages/count_tokens": httpx2.Response(200, json={"input_tokens": 29})}
    )

    count = await api.client().count_tokens(LLMRequest("x", system="s", tier=Tier.FAST))

    assert count == 29
    [body] = api.bodies("/v1/messages/count_tokens")
    assert body["model"] == "claude-haiku-5-5"
    assert body["system"] == [{"type": "text", "text": "s"}]


async def test_batch_request_ids_are_checked_before_sending() -> None:
    api = FakeAnthropic()
    client = api.client()
    for ids in ([], ["a", "a"], ["has space"], ["x" * 65]):
        with pytest.raises(ValueError, match="batch"):
            await client.submit_batch([(i, LLMRequest("x")) for i in ids])
    assert api.requests == []


async def test_a_batch_is_sent_polled_and_its_results_read() -> None:
    path = "/v1/messages/batches"
    states = iter([batch("msgbatch_1", "ended", succeeded=2, errored=1, expired=1)] * 2)
    api = FakeAnthropic(
        {
            path: httpx2.Response(200, json=batch("msgbatch_1")),
            f"{path}/msgbatch_1": lambda _: httpx2.Response(200, json=next(states)),
            f"{path}/msgbatch_1/results": jsonl(
                {
                    "custom_id": "ok",
                    "result": {"type": "succeeded", "message": message('{"summary": "s"}')},
                },
                {
                    "custom_id": "prose",
                    "result": {"type": "succeeded", "message": message("not json")},
                },
                {
                    "custom_id": "bad",
                    "result": {
                        "type": "errored",
                        "error": {
                            "type": "error",
                            "error": {"type": "invalid_request_error", "message": "too long"},
                        },
                    },
                },
                {"custom_id": "late", "result": {"type": "expired"}},
            ),
        }
    )
    client = api.client()
    request = LLMRequest("x", tier=Tier.FAST, max_tokens=500, schema=SCHEMA, effort="low")

    sent = await client.submit_batch([("ok", request), ("prose", request)])
    assert (sent.id, sent.status) == ("msgbatch_1", "in_progress")
    [body] = api.bodies(path)
    params = body["requests"][0]["params"]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["format"]["schema"] == SCHEMA
    assert "fallbacks" not in params  # batches can't fall back

    state = await client.batch_state("msgbatch_1")
    assert (state.status, state.succeeded, state.errored, state.expired) == ("ended", 2, 1, 1)

    items = {
        item.custom_id: item async for item in client.batch_results("msgbatch_1", json_output=True)
    }
    assert items["ok"].result is not None
    assert items["ok"].result.data == {"summary": "s"}
    assert items["ok"].usage == (LLMUsage("anthropic", "claude-sonnet-5-5", 100, 20, batch=True),)
    assert isinstance(items["prose"].error, LLMBadOutput)
    assert items["prose"].usage  # billed all the same
    assert (items["bad"].outcome, items["bad"].usage) == ("errored", ())
    assert items["bad"].error is not None
    assert "too long" in items["bad"].error.message
    assert items["bad"].error.status == 400  # rejected: sending it again won't help
    assert (items["late"].outcome, items["late"].result, items["late"].error) == (
        "expired",
        None,
        None,
    )


# ---------------------------------------------------------------- batches in the database


async def _org(db: AsyncSession) -> uuid.UUID:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    await set_org_context(db, access.org.id)
    return access.org.id


def _batch_api(*results: dict[str, Any]) -> tuple[FakeAnthropic, list[str]]:
    """A fake API whose batch ``msgbatch_1`` runs through ``statuses`` as it is polled."""
    path = "/v1/messages/batches"
    statuses = ["in_progress", "ended"]
    counts = {"succeeded": len(results)}

    def state(_: httpx2.Request) -> httpx2.Response:
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return httpx2.Response(200, json=batch("msgbatch_1", status, **counts))

    api = FakeAnthropic(
        {
            path: httpx2.Response(200, json=batch("msgbatch_1")),
            f"{path}/msgbatch_1": state,
            f"{path}/msgbatch_1/results": jsonl(*results),
        }
    )
    return api, statuses


def _succeeded(custom_id: str, text: str) -> dict[str, Any]:
    usage = {
        "input_tokens": 50_000,
        "output_tokens": 10_000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    answer = message(text, model="claude-haiku-5-5", usage=usage)
    return {"custom_id": custom_id, "result": {"type": "succeeded", "message": answer}}


@pytest.mark.integration
async def test_a_batch_is_tracked_until_its_results_are_collected_once(
    db_session: AsyncSession,
) -> None:
    org_id = await _org(db_session)
    api, _ = _batch_api(
        _succeeded("f1", '{"summary": "one"}'), _succeeded("f2", '{"summary": "two"}')
    )
    client = api.client()
    request = LLMRequest("x", tier=Tier.FAST, schema=SCHEMA)

    row = await batches.submit(
        db_session,
        client,
        [("f1", request), ("f2", request)],
        org_id=org_id,
        kind="summaries",
        ref_type="index_job",
        ref_id="job-1",
    )
    assert (row.model, row.request_count, row.status) == (
        "claude-haiku-5-5",
        2,
        LLMBatchStatus.IN_PROGRESS,
    )

    # A restarted worker finds it, polls it until it ends, then collects it.
    [found] = await batches.uncollected(db_session, kind="summaries", ref_id="job-1")
    assert found.id == row.id
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    await batches.wait(db_session, client, found, poll_seconds=30, sleep=sleep)
    assert (found.status, found.succeeded, slept) == (LLMBatchStatus.ENDED, 2, [30])
    assert found.ended_at is not None

    sink = usage_ledger(
        db_session,
        CostCalculator(DEFAULT_PRICE_TABLE),
        org_id=org_id,
        ref_type="index_job",
        ref_id="job-1",
    )
    items = [
        i async for i in batches.collect(db_session, client, found, json_output=True, on_usage=sink)
    ]
    assert sorted(i.result.data["summary"] for i in items if i.result) == ["one", "two"]
    assert found.collected_at is not None
    assert await batches.uncollected(db_session, kind="summaries") == []
    with pytest.raises(ValueError, match="already collected"):
        _ = [i async for i in batches.collect(db_session, client, found)]

    # Haiku 5.5 lists at $0.10 / $0.50 per million tokens in / out; batches are half price.
    rows = (await db_session.scalars(select(UsageEvent).where(UsageEvent.org_id == org_id))).all()
    assert [(r.kind, r.model, r.batch, r.cost_usd) for r in rows] == [
        (UsageKind.LLM, "claude-haiku-5-5", True, Decimal("0.005000"))
    ] * 2


@pytest.mark.integration
async def test_a_running_batch_cannot_be_collected(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    api, _ = _batch_api()
    client = api.client()
    row = await batches.submit(
        db_session, client, [("a", LLMRequest("x"))], org_id=org_id, kind="summaries"
    )
    with pytest.raises(ValueError, match="hasn't ended"):
        _ = [i async for i in batches.collect(db_session, client, row)]


@pytest.mark.integration
async def test_a_batch_uses_one_model(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    api, _ = _batch_api()
    mixed = [("a", LLMRequest("x", tier=Tier.FAST)), ("b", LLMRequest("x", tier=Tier.DEEP))]
    with pytest.raises(ValueError, match="one model tier"):
        await batches.submit(db_session, api.client(), mixed, org_id=org_id, kind="summaries")
    assert api.requests == []


@pytest.mark.integration
async def test_batches_are_org_scoped(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    api, _ = _batch_api()
    await batches.submit(
        db_session, api.client(), [("a", LLMRequest("x"))], org_id=org_id, kind="summaries"
    )
    assert len(await batches.uncollected(db_session, kind="summaries")) == 1
    await set_org_context(db_session, uuid.uuid4())  # another org
    assert await batches.uncollected(db_session, kind="summaries") == []
