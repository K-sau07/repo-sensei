"""File summaries (requests, answers, the cost guard, the batch run with its retry), the glossary
and directory rollups, against a fake Anthropic API."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    GlossaryTerm,
    LLMBatch,
    LLMBatchStatus,
    Repo,
    RepoSource,
    Snapshot,
    UsageEvent,
    UsageKind,
)
from sensei_core.orgs.service import OrgService
from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator, month_start, record_usage
from sensei_engine.llm import batches, summaries
from sensei_engine.llm.anthropic_client import CUSTOM_ID, LLMResult, LLMUsage
from sensei_engine.llm.glossary import build_glossary, normalize_term, save_glossary
from sensei_engine.llm.rollups import summarize_directories, to_json, tree
from sensei_engine.llm.summaries import (
    PROMPT_VERSION,
    CostCapExceeded,
    CostEstimate,
    FileInput,
    FileSummary,
    SymbolInput,
    SymbolSummary,
    answer_schema,
    build_request,
    build_requests,
    check_budget,
    collect_summaries,
    estimate_cost,
    parse_answer,
    render,
    summarize_files,
    system_prompt,
    terms,
)
from sensei_testing import make_user
from sensei_testing.anthropic import FakeAnthropic, batch, jsonl, message

CALCULATOR = CostCalculator(DEFAULT_PRICE_TABLE)
BATCHES = "/v1/messages/batches"

ITEMS = """from app.models import Item


class ItemService:
    def create(self, owner_id, data):
        item = Item(owner_id=owner_id, **data)
        return item


MAX_ITEMS = 100
"""


def _file(path: str = "backend/app/items.py", content: str = ITEMS) -> FileInput:
    return FileInput(
        path,
        "python",
        content,
        (
            SymbolInput(f"{path}::", "module", "items", 1, 10),
            SymbolInput(f"{path}::ItemService", "class", "ItemService", 4, 7, "class ItemService"),
            SymbolInput(
                f"{path}::ItemService.create",
                "method",
                "create",
                5,
                7,
                "def create(self, owner_id, data)",
            ),
            SymbolInput(f"{path}::MAX_ITEMS", "variable", "MAX_ITEMS", 10, 10),
        ),
        ("fastapi", "sqlmodel"),
    )


def _answer(file: FileInput, **extra: Any) -> dict[str, Any]:
    asked = summaries.summarizable(file.symbols)
    symbols = [
        {"key": handle, "summary": f"{s.name} does things", "domain_terms": ["Item", "owner"]}
        for handle, s in zip(summaries.handles(asked), asked, strict=True)
    ]
    return {"file_summary": f"{file.path} handles items", "symbols": symbols, **extra}


def _result(data: Any) -> LLMResult:
    return LLMResult(json.dumps(data), data, "claude-haiku-5-5", ())


# ---------------------------------------------------------------- requests


def test_a_request_shows_the_file_its_symbols_and_numbered_lines() -> None:
    file = _file()
    prompt, asked, cut = render(file)
    assert not cut
    # The module and variables aren't asked about (the file summary covers them).
    assert [s.name for s in asked] == ["ItemService", "create"]
    assert prompt.startswith(
        "<path>backend/app/items.py</path>\n<language>python</language>\n"
        "<frameworks>fastapi, sqlmodel</frameworks>\n<symbols>\n"
        "s1\tclass ItemService (lines 4-7) class ItemService\n"
        "s2\tmethod create (lines 5-7) def create(self, owner_id, data)\n</symbols>"
    )
    assert "5\t    def create(self, owner_id, data):" in prompt
    request = build_request(file)
    assert request.system == system_prompt()
    assert request.schema == answer_schema()
    assert request.effort == "low"
    assert request.max_tokens == 150 + 2 * 130 + 2_000
    assert request.cache_system


def test_long_content_is_cut_and_the_symbols_past_the_cut_are_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(summaries, "MAX_PROMPT_CHARS", 60)
    prompt, asked, cut = render(_file())
    assert cut
    assert prompt.endswith("(cut short after line 4 of 10)")
    assert [s.name for s in asked] == ["ItemService"]
    summary, dropped = parse_answer(_file(), _result(_answer(_file())))
    assert summary.truncated
    assert list(summary.symbols) == ["backend/app/items.py::ItemService"]
    assert dropped == 1  # the answer's entry for create, which wasn't asked about


def test_custom_ids_come_from_the_path_and_paths_are_unique() -> None:
    first, second = _file("a/b.py"), _file("a/c.py")
    assert first.custom_id == _file("a/b.py").custom_id != second.custom_id
    assert CUSTOM_ID.fullmatch(first.custom_id)
    assert [custom_id for custom_id, _ in build_requests([first, second])] == [
        first.custom_id,
        second.custom_id,
    ]
    with pytest.raises(ValueError, match="listed twice"):
        build_requests([first, first])


# ---------------------------------------------------------------- answers


def test_an_answer_keeps_only_the_files_own_symbols_once() -> None:
    file = _file()
    answer = _answer(file)
    answer["symbols"] += [
        {"key": "s9", "summary": "a handle it wasn't given", "domain_terms": []},
        {"key": "s1", "summary": "again", "domain_terms": []},
        {"key": f"{file.path}::ItemService", "summary": "the real key", "domain_terms": []},
        {"key": " s2 ", "summary": "repeated, spaced", "domain_terms": []},
    ]
    answer["symbols"][1]["summary"] = "  Creates an   item\n for its owner. "
    summary, dropped = parse_answer(file, _result(answer))
    assert dropped == 4
    assert summary == FileSummary(
        file.path,
        "backend/app/items.py handles items",
        {
            f"{file.path}::ItemService": SymbolSummary(
                f"{file.path}::ItemService", "ItemService does things", ("Item", "owner")
            ),
            f"{file.path}::ItemService.create": SymbolSummary(
                f"{file.path}::ItemService.create",
                "Creates an item for its owner.",
                ("Item", "owner"),
            ),
        },
        PROMPT_VERSION,
        "claude-haiku-5-5",
    )


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"symbols": []},
        {"file_summary": "x", "symbols": [{"key": "k"}]},
        {"file_summary": "   ", "symbols": []},
        "just text",
    ],
    ids=["nothing", "no-file-summary", "symbol-fields", "empty-summary", "text"],
)
def test_an_unusable_answer_is_refused(data: Any) -> None:
    with pytest.raises(ValueError, match=r"schema|no file summary"):
        parse_answer(_file(), _result(data))


def test_domain_terms_are_tidied() -> None:
    raw = [" password  reset ", "Password Reset", "`token`", "x", "y" * 61, "a", "b1", "c2"]
    raw += ["d3", "e4", "f5", "g6"]
    assert terms(raw) == ("password reset", "token", "b1", "c2", "d3", "e4")


# ---------------------------------------------------------------- the cost guard


async def test_the_estimate_counts_every_request_when_there_are_few() -> None:
    def count(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        return httpx2.Response(200, json={"input_tokens": len(body["messages"][0]["content"])})

    api = FakeAnthropic({"/v1/messages/count_tokens": count})
    requests = [build_request(_file(f"m{i}.py")) for i in range(3)]
    estimate = await estimate_cost(api.client(), requests, CALCULATOR)
    assert len(api.bodies("/v1/messages/count_tokens")) == 3
    # The schema is counted with each request (count_tokens sees output_config).
    assert "output_config" in api.bodies("/v1/messages/count_tokens")[0]
    assert estimate.counted == 3
    assert estimate.input_tokens == sum(len(r.prompt) for r in requests)
    assert estimate.output_tokens == 3 * (150 + 2 * 130)
    each = CALCULATOR.cost(
        "anthropic",
        "claude-haiku-5-5",
        tokens_in=len(requests[0].prompt),
        tokens_out=150 + 2 * 130,
        batch=True,
    )
    assert each is not None
    assert estimate.cost_usd == 3 * each


async def test_each_request_is_priced_at_its_own_size() -> None:
    # 60K tokens each: 180K in all, but no request is over the 100K long-context threshold.
    api = FakeAnthropic(
        {"/v1/messages/count_tokens": httpx2.Response(200, json={"input_tokens": 60_000})}
    )
    requests = [build_request(_file(f"m{i}.py")) for i in range(3)]
    estimate = await estimate_cost(api.client(), requests, CALCULATOR)
    short = CALCULATOR.cost(
        "anthropic", "claude-haiku-5-5", tokens_in=60_000, tokens_out=410, batch=True
    )
    assert short is not None
    assert estimate.cost_usd == 3 * short


async def test_with_many_requests_a_sample_is_counted_and_the_rest_fitted() -> None:
    def count(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        return httpx2.Response(
            200, json={"input_tokens": 700 + len(body["messages"][0]["content"]) // 3}
        )

    api = FakeAnthropic({"/v1/messages/count_tokens": count})
    requests = [
        build_request(_file(f"m{i}.py", ITEMS + "# filler\n" * (i * 40))) for i in range(30)
    ]
    estimate = await estimate_cost(api.client(), requests, CALCULATOR, sample=5)
    assert len(api.bodies("/v1/messages/count_tokens")) == 5
    assert estimate.counted == 5
    truth = sum(700 + len(r.prompt) // 3 for r in requests)
    assert abs(estimate.input_tokens - truth) / truth < 0.01


async def _org(db: AsyncSession, slug: str = "acme") -> uuid.UUID:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name=slug, slug=f"{slug}-{owner.github_id}"
    )
    await set_org_context(db, access.org.id)
    return access.org.id


def _estimate(cost: str) -> CostEstimate:
    return CostEstimate(1, 1000, 100, Decimal(cost), 1)


async def test_the_guard_stops_a_run_that_would_pass_the_months_cap(
    db_session: AsyncSession,
) -> None:
    org_id = await _org(db_session)
    for model in ("claude-sonnet-5-5", "claude-sonnet-5-5"):
        await record_usage(
            db_session, CALCULATOR, org_id=org_id, kind=UsageKind.LLM, provider="anthropic",
            model=model, tokens_in=1_000_000,
        )  # fmt: skip
    spent = await check_budget(db_session, org_id, _estimate("1.5"), Decimal(10))
    assert spent == Decimal(4)  # two million Sonnet input tokens at $2
    with pytest.raises(CostCapExceeded) as stopped:
        await check_budget(db_session, org_id, _estimate("6.01"), Decimal(10))
    assert (stopped.value.spent, stopped.value.cap) == (Decimal(4), Decimal(10))
    # Next month, this month's spend no longer counts.
    next_month = month_start() + timedelta(days=32)
    assert (
        await check_budget(db_session, org_id, _estimate("6.01"), Decimal(10), now=next_month) == 0
    )
    assert month_start(datetime(2026, 3, 31, 23, 0, tzinfo=UTC)) == datetime(2026, 3, 1, tzinfo=UTC)


# ---------------------------------------------------------------- the batch run


def _sessions(db: AsyncSession) -> Callable[[], Any]:
    """Each "transaction" a savepoint of the test's session (rolled back at the end)."""

    @asynccontextmanager
    async def opened() -> AsyncGenerator[AsyncSession]:
        async with db.begin_nested():
            yield db

    return opened


Answer = Callable[[str, dict[str, Any]], dict[str, Any]]  # (custom id, params) -> result line


def _batch_api(
    answer: Answer, *, retry: Callable[[dict[str, Any]], str] | None = None
) -> FakeAnthropic:
    """A batch API that answers each submitted request with ``answer``; ``/v1/messages``
    (the retry) answers with ``retry``'s text."""
    statuses = ["in_progress", "ended"]
    api = FakeAnthropic()

    def submitted() -> list[dict[str, Any]]:
        return api.bodies(BATCHES)[0]["requests"]

    def state(_: httpx2.Request) -> httpx2.Response:
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return httpx2.Response(200, json=batch("msgbatch_1", status, succeeded=len(submitted())))

    def results(_: httpx2.Request) -> httpx2.Response:
        return jsonl(*(answer(r["custom_id"], r["params"]) for r in submitted()))

    def single(request: httpx2.Request) -> httpx2.Response:
        assert retry is not None
        return httpx2.Response(200, json=message(retry(json.loads(request.content))))

    def count(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"input_tokens": 2_000})

    api.routes.update(
        {
            BATCHES: httpx2.Response(200, json=batch("msgbatch_1")),
            f"{BATCHES}/msgbatch_1": state,
            f"{BATCHES}/msgbatch_1/results": results,
            "/v1/messages": single,
            "/v1/messages/count_tokens": count,
        }
    )
    return api


def _line(custom_id: str, text: str) -> dict[str, Any]:
    usage = {
        "input_tokens": 2_000,
        "output_tokens": 300,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    answer = message(text, model="claude-haiku-5-5", usage=usage)
    return {"custom_id": custom_id, "result": {"type": "succeeded", "message": answer}}


def _path_of(params: dict[str, Any]) -> str:
    found = re.search(r"<path>(.*)</path>", params["messages"][0]["content"])
    assert found is not None
    return found[1]


@pytest.mark.integration
async def test_files_are_summarized_in_one_batch_and_a_bad_answer_is_retried_once(
    db_session: AsyncSession,
) -> None:
    org_id = await _org(db_session)
    files = [_file("a/good.py"), _file("a/garbled.py"), _file("a/hopeless.py")]
    by_path = {file.path: file for file in files}

    def answer(custom_id: str, params: dict[str, Any]) -> dict[str, Any]:
        path = _path_of(params)
        if path == "a/good.py":
            data = _answer(by_path[path])
            data["symbols"].append({"key": "made::up", "summary": "x", "domain_terms": []})
            return _line(custom_id, json.dumps(data))
        if path == "a/garbled.py":
            return _line(custom_id, '{"file_summary": "cut')
        return {"custom_id": custom_id, "result": {"type": "errored", "error": {
            "type": "error", "error": {"type": "api_error", "message": "boom"}}}}  # fmt: skip

    def retry(body: dict[str, Any]) -> str:
        path = _path_of(body)
        return json.dumps(_answer(by_path[path])) if path == "a/garbled.py" else "still not json"

    api = _batch_api(answer, retry=retry)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    run = await summarize_files(
        _sessions(db_session), api.client(), files, org_id=org_id, calculator=CALCULATOR,
        cap_usd=Decimal(5), ref_type="index_job", ref_id="job-1", poll_seconds=30, sleep=sleep,
    )  # fmt: skip

    assert sorted(run.summaries) == ["a/garbled.py", "a/good.py"]
    assert list(run.skipped) == ["a/hopeless.py"]
    assert run.skipped["a/hopeless.py"].startswith("retried: ")
    assert run.retried == 2
    assert run.dropped_keys == 1
    assert slept == [30]
    assert run.summaries["a/good.py"].prompt_version == PROMPT_VERSION
    # One batch of three, then one retry each for the two that failed.
    [sent] = api.bodies(BATCHES)
    assert [r["custom_id"] for r in sent["requests"]] == [f.custom_id for f in files]
    assert len(api.bodies("/v1/messages")) == 2
    row = await db_session.scalar(select(LLMBatch).where(LLMBatch.id == run.batch_id))
    assert row is not None
    assert row.status is LLMBatchStatus.ENDED
    assert row.collected_at is not None
    assert run.estimate is not None
    assert row.estimated_cost_usd == run.estimate.cost_usd > 0
    usage = (
        await db_session.execute(
            select(UsageEvent.batch, func.count())
            .where(UsageEvent.org_id == org_id, UsageEvent.ref_id == "job-1")
            .group_by(UsageEvent.batch)
        )
    ).all()
    # Two batch answers billed (the errored one wasn't), and the two retries.
    assert sorted(usage) == [(False, 2), (True, 2)]


@pytest.mark.integration
async def test_over_the_cap_nothing_is_recorded_or_sent(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    api = _batch_api(lambda custom_id, _: _line(custom_id, "{}"))
    with pytest.raises(CostCapExceeded):
        await summarize_files(
            _sessions(db_session), api.client(), [_file()], org_id=org_id,
            calculator=CALCULATOR, cap_usd=Decimal(0),
        )  # fmt: skip
    assert api.bodies(BATCHES) == []
    assert await db_session.scalar(select(func.count()).select_from(LLMBatch)) == 0


@pytest.mark.integration
async def test_after_a_restart_results_are_matched_by_path(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    files = [_file("x/one.py"), _file("x/two.py")]
    by_id = {file.custom_id: file for file in files}
    api = _batch_api(lambda custom_id, _: _line(custom_id, json.dumps(_answer(by_id[custom_id]))))
    client = api.client()
    requests = build_requests(files)
    row = await batches.reserve(db_session, client, requests, org_id=org_id, kind="summaries")
    await batches.send(db_session, client, row.id, requests)
    await batches.refresh(db_session, client, row)
    await batches.refresh(db_session, client, row)
    # A new worker knows the files, in another order, and nothing else.
    run = await collect_summaries(
        db_session, client, row, list(reversed(files)), calculator=CALCULATOR, org_id=org_id
    )
    assert sorted(run.summaries) == ["x/one.py", "x/two.py"]
    assert run.skipped == {}


# ---------------------------------------------------------------- glossary


def _summary(path: str, symbols: dict[str, tuple[str, ...]]) -> FileSummary:
    return FileSummary(
        path,
        "s",
        {key: SymbolSummary(key, "s", found) for key, found in symbols.items()},
        PROMPT_VERSION,
        "claude-haiku-5-5",
    )


def test_the_glossary_maps_each_term_to_the_symbols_that_use_it() -> None:
    ids = {key: uuid.uuid4() for key in ("a::one", "a::two", "b::three")}
    found = build_glossary(
        [
            _summary(
                "a.py", {"a::one": ("Password Reset", "token"), "a::two": ("password  reset",)}
            ),
            _summary("b.py", {"b::three": ("token", "invoice"), "b::nodes": ("ghost",)}),
        ],
        ids,
    )
    assert [(e.term, e.weight) for e in found] == [
        ("password reset", 2.0), ("token", 2.0), ("invoice", 1.0),
    ]  # fmt: skip
    assert found[0].symbol_ids == (ids["a::one"], ids["a::two"])
    assert normalize_term("  Flow   RUN ") == "flow run"


async def _snapshot(db: AsyncSession, org_id: uuid.UUID) -> Snapshot:
    repo = Repo(
        org_id=org_id, source=RepoSource.PUBLIC_URL, github_repo_id=7, owner="o", name="r",
        full_name="o/r", slug="r", default_branch="main", visibility="public",
    )  # fmt: skip
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(org_id=org_id, repo_id=repo.id, ref="main", commit_sha="a" * 40)
    db.add(snapshot)
    await db.flush()
    return snapshot


@pytest.mark.integration
async def test_a_snapshots_glossary_is_replaced_and_private_to_its_org(
    db_session: AsyncSession,
) -> None:
    org_id = await _org(db_session)
    snapshot = await _snapshot(db_session, org_id)
    one, two = uuid.uuid4(), uuid.uuid4()
    entries = build_glossary(
        [_summary("a.py", {"k1": ("invoice",), "k2": ("invoice", "tax")})], {"k1": one, "k2": two}
    )
    where = {"org_id": org_id, "repo_id": snapshot.repo_id, "snapshot_id": snapshot.id}
    assert await save_glossary(db_session, **where, entries=entries) == 2
    assert await save_glossary(db_session, **where, entries=entries[:1]) == 1  # replaced
    rows = (await db_session.scalars(select(GlossaryTerm))).all()
    assert [(r.term, r.symbol_ids, r.weight) for r in rows] == [("invoice", [one, two], 2.0)]

    await _org(db_session, "other")  # now scoped to another org
    assert (await db_session.scalars(select(GlossaryTerm))).all() == []


# ---------------------------------------------------------------- rollups


def test_the_tree_has_every_directory_above_the_files() -> None:
    found = tree(["README.md", "backend/app/main.py", "backend/app/api/items.py"])
    assert found["."].files == ["README.md"]
    assert found["."].directories == {"backend"}
    assert found["backend"].directories == {"backend/app"}
    assert found["backend/app"].files == ["backend/app/main.py"]
    assert found["backend/app/api"].files == ["backend/app/api/items.py"]


def _directory(text: str) -> str:
    found = re.search(r"<directory>(.*)</directory>", text)
    assert found is not None
    return found[1]


async def test_directories_are_summarized_deepest_first_from_their_children() -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        text: str = json.loads(request.content)["messages"][0]["content"]
        directory = _directory(text)
        if directory == "frontend/src":
            return httpx2.Response(200, json=message("not json"))
        summary = {"summary": f"SUM[{directory}] " + " | ".join(text.splitlines()[2:-1])}
        return httpx2.Response(200, json=message(json.dumps(summary)))

    api = FakeAnthropic({"/v1/messages": answer})
    billed: list[LLMUsage] = []

    async def sink(usage: LLMUsage) -> None:
        billed.append(usage)

    rollups = await summarize_directories(
        api.client(),
        {
            "backend/app/api/items.py": "item routes",
            "backend/app/main.py": "the app",
            "frontend/src/main.tsx": "the ui",
            "README.md": "the readme",
        },
        on_usage=sink,
    )
    asked = [_directory(body["messages"][0]["content"]) for body in api.bodies("/v1/messages")]
    # Deepest first; backend/ only holds app/, so it takes app's summary without a call.
    assert asked[0] == "backend/app/api"
    assert set(asked[1:3]) == {"backend/app", "frontend/src"}
    assert set(asked) == {"backend/app/api", "frontend/src", "backend/app", "(repository root)"}
    assert len(billed) == len(asked)
    app = rollups["backend/app"]
    assert "api/: SUM[backend/app/api] items.py: item routes" in app.summary
    assert "main.py: the app" in app.summary
    assert rollups["backend"].inherited_from == "backend/app"
    assert rollups["backend"].summary == app.summary
    # frontend/src's call failed, so frontend/ (which holds only src/) has nothing to take.
    assert "frontend/src" not in rollups
    assert "frontend" not in rollups
    root = rollups["."].summary
    assert "frontend/" in root
    assert "frontend/:" not in root  # listed by name only
    assert "README.md: the readme" in root
    artifact = json.loads(to_json(rollups))
    assert artifact["prompt_version"] == "summarize_directory.v1"
    assert [d["path"] for d in artifact["directories"]] == sorted(rollups)
