"""File summaries (requests, answers, the cost guard, the batch run with its retry), the glossary
and directory rollups, against a fake Anthropic API."""

from __future__ import annotations

import asyncio
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
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sensei_core.config import Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import (
    GlossaryTerm,
    LLMBatch,
    LLMBatchStatus,
    Org,
    Repo,
    RepoSource,
    Snapshot,
    UsageEvent,
    UsageKind,
)
from sensei_core.orgs.service import OrgService
from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator, month_start, record_usage
from sensei_engine.llm import batches, summaries
from sensei_engine.llm import rollups as rollup_module
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
    from_json,
    parse_answer,
    render,
    reserve_batch,
    retry_failed,
    summarize_files,
    system_prompt,
    terms,
)
from sensei_engine.llm.summaries import to_json as summaries_json
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


@pytest.mark.integration
async def test_the_guard_counts_what_uncollected_batches_will_cost(
    db_session: AsyncSession,
) -> None:
    org_id = await _org(db_session)
    client = FakeAnthropic().client()
    requests = build_requests([_file()])
    for status, collected, cost in (
        (LLMBatchStatus.IN_PROGRESS, False, "2"),  # running: its cost isn't in the ledger yet
        (LLMBatchStatus.ENDED, False, "3"),  # ended, not collected: nor is this one's
        (LLMBatchStatus.ENDED, True, "100"),  # collected: its usage is in the ledger
        (LLMBatchStatus.FAILED, False, "100"),  # never ran
    ):
        row = await batches.reserve(db_session, client, requests, org_id=org_id, kind="summaries")
        row.status, row.estimated_cost_usd = status, Decimal(cost)
        row.collected_at = datetime.now(UTC) if collected else None
    await db_session.flush()
    assert await summaries.pending_estimates(db_session, org_id) == Decimal(5)
    assert await check_budget(db_session, org_id, _estimate("5"), Decimal(10)) == Decimal(5)
    with pytest.raises(CostCapExceeded):
        await check_budget(db_session, org_id, _estimate("5.01"), Decimal(10))


@pytest.fixture
async def committed_org(migrated_test_db: Settings) -> AsyncGenerator[uuid.UUID]:
    """An org committed by the system role (two connections see it), removed afterwards."""
    system = create_async_engine(migrated_test_db.database.system_url.get_secret_value())
    org_id = uuid.uuid4()
    async with system.begin() as conn:
        await conn.execute(insert(Org).values(id=org_id, slug=f"cap-{org_id.hex[:8]}", name="C"))
    try:
        yield org_id
    finally:
        async with system.begin() as conn:
            await conn.execute(delete(Org).where(Org.id == org_id))
        await system.dispose()


@pytest.mark.integration
async def test_two_runs_at_the_cap_at_once_reserve_one_batch(
    migrated_test_db: Settings, committed_org: uuid.UUID
) -> None:
    """Each run alone fits under the cap, both together don't: the org's lock makes the second
    check wait for the first reservation to commit, and then see it."""
    org_id = committed_org
    engine = create_async_engine(migrated_test_db.database.url.get_secret_value())
    client = FakeAnthropic().client()
    requests = build_requests([_file()])
    first_reserved, first_may_commit = asyncio.Event(), asyncio.Event()
    outcomes: dict[str, str] = {}

    async def run(name: str) -> None:
        async with AsyncSession(engine, expire_on_commit=False) as db, db.begin():
            await set_org_context(db, org_id)
            try:
                await reserve_batch(
                    db, client, requests, _estimate("6"), org_id=org_id, cap_usd=Decimal(10),
                    ref_type="index_job", ref_id=name,
                )  # fmt: skip
            except CostCapExceeded as exc:
                outcomes[name] = f"over the cap (spent {exc.spent:.2f})"
                return
            outcomes[name] = "reserved"
            if name == "first":
                first_reserved.set()
                await first_may_commit.wait()  # hold the lock while the second checks

    first = asyncio.create_task(run("first"))
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_reserved.wait(), timeout=10)
        second = asyncio.create_task(run("second"))
        await asyncio.sleep(0.3)
        assert not second.done(), "the second check must wait for the first reservation"
        first_may_commit.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    finally:
        first_may_commit.set()  # never leave a transaction open: cleanup would wait on it
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await engine.dispose()
    assert outcomes == {"first": "reserved", "second": "over the cap (spent 6.00)"}


@pytest.mark.integration
async def test_two_runs_at_the_cap_racing_reserve_one_batch(
    migrated_test_db: Settings, committed_org: uuid.UUID
) -> None:
    org_id = committed_org
    client = FakeAnthropic().client()
    requests = build_requests([_file()])
    engine = create_async_engine(migrated_test_db.database.url.get_secret_value())
    raced: list[str] = []

    async def race(name: str) -> None:
        async with AsyncSession(engine, expire_on_commit=False) as db, db.begin():
            await set_org_context(db, org_id)
            try:
                await reserve_batch(
                    db, client, requests, _estimate("6"), org_id=org_id, cap_usd=Decimal(10),
                    ref_type="index_job", ref_id=name,
                )  # fmt: skip
                raced.append("reserved")
            except CostCapExceeded:
                raced.append("over the cap")

    try:
        await asyncio.wait_for(asyncio.gather(race("a"), race("b")), timeout=10)
    finally:
        await engine.dispose()
    assert sorted(raced) == ["over the cap", "reserved"]


def test_the_rollups_estimate_counts_a_call_per_directory_that_needs_one() -> None:
    paths = ["README.md", "backend/app/main.py", "backend/app/api/items.py"]
    found = rollup_module.estimate_cost(paths, CALCULATOR, "claude-haiku-5-5")
    # The root, backend/app and backend/app/api; backend/ only holds app/ and takes its summary.
    assert found.calls == 3
    assert found.output_tokens == 3 * rollup_module.ESTIMATED_OUTPUT_TOKENS
    assert found.input_tokens > 3 * len(rollup_module.system_prompt()) // 3
    expected = sum(
        (
            CALCULATOR.cost("anthropic", "claude-haiku-5-5", tokens_in=i, tokens_out=400)
            or Decimal(0)
            for i in _rollup_inputs(paths)
        ),
        Decimal(0),
    )
    assert found.cost_usd == expected > 0
    with pytest.raises(ValueError, match="no price"):
        rollup_module.estimate_cost(paths, CALCULATOR, "claude-unknown-1")


def _rollup_inputs(paths: list[str]) -> list[int]:
    system = len(rollup_module.system_prompt())
    children = {".": 2, "backend/app": 2, "backend/app/api": 1}  # files and directories listed
    return [
        -(-(system + len(directory) + count * 440) // 3) for directory, count in children.items()
    ]


# ---------------------------------------------------------------- the batch run


def _sessions(db: AsyncSession) -> Callable[[], Any]:
    """Each "transaction" a savepoint of the test's session (rolled back at the end), one at a
    time: concurrent callers, each with a session of its own in a worker, take turns here."""
    turn = asyncio.Lock()

    @asynccontextmanager
    async def opened() -> AsyncGenerator[AsyncSession]:
        async with turn, db.begin_nested():
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
async def test_a_run_restarted_mid_batch_takes_up_its_batch_and_bills_once(
    db_session: AsyncSession,
) -> None:
    """Spec 2.8.8: the worker dies while the batch runs; the redelivered run finds the batch
    open for its reference, waits for it and collects it, without sending another."""
    org_id = await _org(db_session)
    files = [_file("r/one.py"), _file("r/two.py")]
    by_id = {file.custom_id: file for file in files}
    api = _batch_api(lambda custom_id, _: _line(custom_id, json.dumps(_answer(by_id[custom_id]))))
    stored: list[list[str]] = []

    async def store(db: AsyncSession, run: summaries.SummaryRun) -> None:
        claimed = await db.scalar(select(LLMBatch.collected_at).where(LLMBatch.id == run.batch_id))
        assert claimed is not None  # in the claiming transaction
        stored.append(sorted(run.summaries))

    async def die(_: float) -> None:
        raise RuntimeError("the worker went away")

    async def sleep(_: float) -> None:
        return None

    common: dict[str, Any] = {
        "org_id": org_id, "calculator": CALCULATOR, "cap_usd": Decimal(5), "store": store,
        "ref_type": "index_job", "ref_id": "job-r",
    }  # fmt: skip
    with pytest.raises(RuntimeError):
        await summarize_files(_sessions(db_session), api.client(), files, sleep=die, **common)
    assert len(api.bodies(BATCHES)) == 1  # sent, still running
    run = await summarize_files(_sessions(db_session), api.client(), files, sleep=sleep, **common)

    assert len(api.bodies(BATCHES)) == 1  # not sent again
    assert sorted(run.summaries) == ["r/one.py", "r/two.py"]
    assert run.estimate is None  # nothing estimated: nothing new was sent
    assert stored == [["r/one.py", "r/two.py"]]
    billed = await db_session.scalar(
        select(func.count()).select_from(UsageEvent).where(UsageEvent.ref_id == "job-r")
    )
    assert billed == 2
    assert await db_session.scalar(select(func.count()).select_from(LLMBatch)) == 1


@pytest.mark.integration
async def test_a_send_that_never_went_out_is_settled_then_sent_again(
    db_session: AsyncSession,
) -> None:
    org_id = await _org(db_session)
    files = [_file("u/one.py")]
    by_id = {file.custom_id: file for file in files}
    api = _batch_api(lambda custom_id, _: _line(custom_id, json.dumps(_answer(by_id[custom_id]))))
    created = api.routes[BATCHES]

    def batches_route(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":  # the provider has no batch from the stopped run
            return httpx2.Response(200, json={"data": [], "has_more": False})
        assert isinstance(created, httpx2.Response)
        return created

    api.routes[BATCHES] = batches_route
    client = api.client()
    stuck = await batches.reserve(
        db_session, client, build_requests(files), org_id=org_id, kind="summaries",
        ref_type="index_job", ref_id="job-u",
    )  # fmt: skip
    stuck.created_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()

    async def sleep(_: float) -> None:
        return None

    run = await summarize_files(
        _sessions(db_session), client, files, org_id=org_id, calculator=CALCULATOR,
        cap_usd=Decimal(5), ref_type="index_job", ref_id="job-u", sleep=sleep,
    )  # fmt: skip
    assert list(run.summaries) == ["u/one.py"]
    await db_session.refresh(stuck)
    assert stuck.status is LLMBatchStatus.FAILED
    assert run.batch_id != stuck.id
    assert len(api.bodies(BATCHES)) == 1


@pytest.mark.integration
async def test_a_store_that_fails_leaves_the_batch_to_collect_again(
    db_session: AsyncSession,
) -> None:
    org_id = await _org(db_session)
    files = [_file("s/one.py")]
    by_id = {file.custom_id: file for file in files}
    api = _batch_api(lambda custom_id, _: _line(custom_id, json.dumps(_answer(by_id[custom_id]))))
    client = api.client()
    requests = build_requests(files)
    row = await batches.reserve(db_session, client, requests, org_id=org_id, kind="summaries")
    await batches.send(db_session, client, row.id, requests)
    await batches.refresh(db_session, client, row)
    await batches.refresh(db_session, client, row)

    async def broken(_: AsyncSession, __: summaries.SummaryRun) -> None:
        raise OSError("the artifact store is down")

    with pytest.raises(OSError, match="down"):
        async with db_session.begin_nested():
            await collect_summaries(
                db_session, client, row, files, calculator=CALCULATOR, org_id=org_id,
                store=broken,
            )  # fmt: skip
    await db_session.refresh(row)
    assert row.collected_at is None  # the claim went with the store
    assert await db_session.scalar(select(func.count()).select_from(UsageEvent)) == 0
    run = await collect_summaries(
        db_session, client, row, files, calculator=CALCULATOR, org_id=org_id
    )
    assert list(run.summaries) == ["s/one.py"]


@pytest.mark.integration
async def test_each_retry_is_billed_as_soon_as_it_is_answered(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    files = [_file("q/one.py"), _file("q/two.py")]
    by_path = {file.path: file for file in files}
    second_asked, second_may_answer = asyncio.Event(), asyncio.Event()

    async def single(request: httpx2.Request) -> httpx2.Response:
        path = _path_of(json.loads(request.content))
        if path == "q/two.py":
            second_asked.set()
            await second_may_answer.wait()
        return httpx2.Response(200, json=message(json.dumps(_answer(by_path[path]))))

    api = _batch_api(lambda custom_id, _: _line(custom_id, "{}"))
    api.routes["/v1/messages"] = single
    run = summaries.SummaryRun(skipped={file.path: "errored: boom" for file in files})
    retrying = asyncio.create_task(
        retry_failed(
            _sessions(db_session),
            api.client(),
            run,
            files,
            calculator=CALCULATOR,
            org_id=org_id,
            cap_usd=Decimal(5),
            ref_id="job-q",
            concurrency=1,
        )
    )
    try:
        await asyncio.wait_for(second_asked.wait(), timeout=10)
        # The first answer is in the ledger while the second is still out.
        billed = await db_session.scalar(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.ref_id == "job-q")
        )
        assert billed == 1
    finally:
        second_may_answer.set()
        await asyncio.wait_for(retrying, timeout=10)
    assert sorted(run.summaries) == ["q/one.py", "q/two.py"]


@pytest.mark.integration
async def test_retries_over_the_cap_are_not_sent(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    api = _batch_api(lambda custom_id, _: _line(custom_id, "{}"), retry=lambda _: "{}")
    run = summaries.SummaryRun(skipped={"a/one.py": "errored: boom"})
    await retry_failed(
        _sessions(db_session), api.client(), run, [_file("a/one.py")], calculator=CALCULATOR,
        org_id=org_id, cap_usd=Decimal(0),
    )  # fmt: skip
    assert api.bodies("/v1/messages") == []
    assert run.skipped == {"a/one.py": "not retried: over the monthly cost cap"}
    assert run.retried == 0


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


def test_the_summaries_artifact_round_trips() -> None:
    found = {
        "b.py": _summary("b.py", {"b::f": ("invoice", "tax")}),
        "a.py": FileSummary("a.py", "the a module", {}, PROMPT_VERSION, "claude-haiku-5-5", True),
    }
    data = summaries_json(found, {"c.py": "errored: boom"})
    assert [f["path"] for f in json.loads(data)["files"]] == ["a.py", "b.py"]
    assert from_json(data) == (found, {"c.py": "errored: boom"})


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
