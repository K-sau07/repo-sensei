"""File and symbol summaries (2.8): what each file and each of its symbols does, in the domain's
words, written by Claude through the Message Batches API (half price).

* **Requests** (:func:`build_requests`): one per file, with its path, language, the repository's
  frameworks, its 2.4 symbols (key, kind, name, lines, signature) and its content with line
  numbers. Content past :data:`MAX_PROMPT_CHARS` is cut (a note says so) and the symbols past the
  cut are left out. The prompt is versioned (:data:`PROMPT_VERSION`, kept on every summary).
* **Answers** (:func:`parse_answer`): JSON matching :func:`answer_schema`. The model sees each
  symbol by a short handle (``s1``, ``s2`` ...), not its key: given the keys, it rewrote them
  (``ts a.ts/f().`` came back as ``a.ts/f().``). Handles are checked against the file's symbols
  and mapped back to their keys; unknown and repeated ones are dropped. Domain terms are tidied.
* **Cost guard** (:func:`estimate_cost`, :func:`check_budget`, :func:`reserve_batch`): before
  anything is sent, the input is counted with ``count_tokens`` (every request, or a sample and a
  fit when there are many) and the output estimated per symbol. The run doesn't start if the
  month's spend, plus the estimates of the org's batches not collected yet, plus this estimate
  would pass the org's cap (:class:`CostCapExceeded`). The check and the reservation hold a
  per-org advisory lock to the end of their transaction, so two runs can't both pass on the
  same headroom. The estimate is recorded on the batch.
* **Batch** (:func:`summarize_files`): reserved, sent, polled and collected through
  :mod:`sensei_engine.llm.batches`; usage goes to the ledger, and the summaries to the
  caller's ``store``, in the transaction that claims the batch. Run again for the same
  reference (a redelivered task), it takes up the batch still open for it instead of sending
  (and paying for) another.
  A request that failed or whose answer is unusable (not JSON, cut off, not matching the
  schema) is sent once more on its own, after its own pass through the cost guard; if that
  fails too (or the cap is in the way) the file is skipped, with the reason.

Storing the summaries is the summaries stage's: a JSON artifact per snapshot (:func:`to_json`,
:func:`from_json`) and the glossary; summary-view chunks, embedded, come with 2.6 and 2.7.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.logging import get_logger
from sensei_core.models import LLMBatch, LLMBatchStatus
from sensei_core.usage import CostCalculator, month_start, spent_since
from sensei_engine.llm import batches
from sensei_engine.llm.anthropic_client import (
    PROVIDER,
    AnthropicClient,
    BatchItem,
    LLMError,
    LLMRequest,
    LLMResult,
    LLMUsage,
    Role,
    tier_for,
)
from sensei_engine.llm.ledger import usage_ledger

log = get_logger(__name__)

PROMPT_VERSION = "summarize_file.v1"
KIND = "summaries"  # the batches' kind
# About 80K tokens of content at 3 characters a token: the prompt stays under 100K.
MAX_PROMPT_CHARS = 240_000
MAX_SYMBOLS = 80  # per file, in line order
SKIPPED_KINDS = frozenset({"module", "variable"})  # the file summary covers the module
MAX_TERMS = 6
MAX_TERM_CHARS = 60
# The answer, for max_tokens and the estimate: a file summary and its JSON, then each symbol's.
OUTPUT_TOKENS_PER_FILE = 150
OUTPUT_TOKENS_PER_SYMBOL = 130
THINKING_ROOM = 2_000
COUNT_SAMPLE = 20  # count_tokens calls at most; the rest is fitted
POLL_SECONDS = 30.0
RETRY_CONCURRENCY = 4
# The advisory lock the cost guard holds per org (with the org id's hash): one namespace for every
# check-then-reserve on an org's model spend.
COST_LOCK = "llm_cost_guard"

Sleep = Callable[[float], Awaitable[None]]
Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]
# Stores what a collection produced, in the transaction that claims the batch.
Store = Callable[[AsyncSession, "SummaryRun"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SymbolInput:
    key: str  # the 2.4 symbol key (``nodes.symbol_key``)
    kind: str
    name: str
    start_line: int
    end_line: int
    signature: str | None = None


@dataclass(frozen=True, slots=True)
class FileInput:
    path: str
    language: str
    content: str
    symbols: tuple[SymbolInput, ...] = ()
    frameworks: tuple[str, ...] = ()

    @property
    def custom_id(self) -> str:
        """The batch request id: from the path, so a resumed run maps results back."""
        return "f_" + hashlib.sha256(self.path.encode()).hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class SymbolSummary:
    key: str
    summary: str
    domain_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileSummary:
    path: str
    summary: str
    symbols: Mapping[str, SymbolSummary]  # by symbol key
    prompt_version: str
    model: str
    truncated: bool = False  # the content was cut before it was sent


@dataclass(frozen=True, slots=True)
class CostEstimate:
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    counted: int  # requests whose input count_tokens counted (the rest were fitted)


@dataclass(slots=True)
class SummaryRun:
    summaries: dict[str, FileSummary] = field(default_factory=dict[str, FileSummary])
    skipped: dict[str, str] = field(default_factory=dict[str, str])  # path -> why
    estimate: CostEstimate | None = None
    batch_id: uuid.UUID | None = None
    retried: int = 0
    dropped_keys: int = 0  # symbol keys the model gave that the file doesn't have, or repeated


class CostCapExceeded(Exception):
    def __init__(self, estimate: CostEstimate, spent: Decimal, cap: Decimal) -> None:
        super().__init__(
            f"summaries would cost about ${estimate.cost_usd} on top of ${spent} spent this month"
            f" (cap ${cap})"
        )
        self.estimate = estimate
        self.spent = spent
        self.cap = cap


# ---------------------------------------------------------------- requests


@cache
def system_prompt() -> str:
    path = resources.files("sensei_engine.llm").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


def answer_schema() -> dict[str, Any]:
    """The JSON schema the model answers in."""
    symbol = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "summary": {"type": "string"},
            "domain_terms": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["key", "summary", "domain_terms"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "file_summary": {"type": "string"},
            "symbols": {"type": "array", "items": symbol},
        },
        "required": ["file_summary", "symbols"],
        "additionalProperties": False,
    }


def summarizable(symbols: Iterable[SymbolInput]) -> list[SymbolInput]:
    """The symbols worth a summary, in line order, at most :data:`MAX_SYMBOLS`."""
    kept = [s for s in symbols if s.kind not in SKIPPED_KINDS]
    return sorted(kept, key=lambda s: (s.start_line, s.key))[:MAX_SYMBOLS]


def render(file: FileInput) -> tuple[str, list[SymbolInput], bool]:
    """The user message for ``file``, the symbols it asks about, and whether the content was
    cut (symbols that start past the cut aren't asked about)."""
    lines = file.content.splitlines()
    numbered: list[str] = []
    size = 0
    for number, line in enumerate(lines, start=1):
        entry = f"{number}\t{line}"
        if size + len(entry) + 1 > MAX_PROMPT_CHARS:
            break
        numbered.append(entry)
        size += len(entry) + 1
    cut = len(numbered) < len(lines)
    symbols = [s for s in summarizable(file.symbols) if s.start_line <= len(numbered)]
    head = [f"<path>{file.path}</path>", f"<language>{file.language}</language>"]
    if file.frameworks:
        head.append(f"<frameworks>{', '.join(file.frameworks)}</frameworks>")
    head.append("<symbols>")
    for handle, symbol in zip(handles(symbols), symbols, strict=True):
        signature = f" {symbol.signature}" if symbol.signature else ""
        head.append(
            f"{handle}\t{symbol.kind} {symbol.name} "
            f"(lines {symbol.start_line}-{symbol.end_line}){signature}"
        )
    head.append("</symbols>")
    note = f"\n(cut short after line {len(numbered)} of {len(lines)})" if cut else ""
    body = "\n".join(numbered)
    return "\n".join(head) + f"\n<content>\n{body}\n</content>{note}", symbols, cut


def handles(symbols: Sequence[SymbolInput]) -> list[str]:
    """What the model calls each symbol: ``s1``, ``s2`` ..."""
    return [f"s{index}" for index in range(1, len(symbols) + 1)]


def build_request(file: FileInput) -> LLMRequest:
    prompt, symbols, _ = render(file)
    return LLMRequest(
        prompt,
        system=system_prompt(),
        tier=tier_for(Role.SUMMARY),
        max_tokens=_output_budget(len(symbols)) + THINKING_ROOM,
        effort="low",
        schema=answer_schema(),
        cache_system=True,
    )


def build_requests(files: Iterable[FileInput]) -> list[tuple[str, LLMRequest]]:
    """One ``(custom id, request)`` per file. Paths must be unique."""
    requests: dict[str, LLMRequest] = {}
    for file in files:
        if file.custom_id in requests:
            msg = f"{file.path} is listed twice"
            raise ValueError(msg)
        requests[file.custom_id] = build_request(file)
    return list(requests.items())


def _output_budget(symbols: int) -> int:
    return OUTPUT_TOKENS_PER_FILE + OUTPUT_TOKENS_PER_SYMBOL * symbols


# ---------------------------------------------------------------- answers


class _Symbol(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    summary: str
    domain_terms: list[str]


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_summary: str
    symbols: list[_Symbol]


def _tidy(text: str) -> str:
    return " ".join(text.split())


def terms(raw: Iterable[str]) -> tuple[str, ...]:
    """Domain terms, tidied: single spaces, no surrounding punctuation, at most
    :data:`MAX_TERMS`, repeats (any case) dropped."""
    found: dict[str, str] = {}
    for term in raw:
        tidy = _tidy(term).strip(" .,;:!?\"'`()[]")
        if 2 <= len(tidy) <= MAX_TERM_CHARS and tidy.casefold() not in found:
            found[tidy.casefold()] = tidy
    return tuple(found.values())[:MAX_TERMS]


def parse_answer(file: FileInput, result: LLMResult) -> tuple[FileSummary, int]:
    """The summary in ``result`` and how many symbol entries were dropped (unknown or repeated
    keys, empty summaries). Raises ValueError when the answer is unusable."""
    try:
        answer = _Answer.model_validate(result.data)
    except ValidationError as exc:
        msg = "the answer doesn't match the schema"
        raise ValueError(msg) from exc
    summary = _tidy(answer.file_summary)
    if not summary:
        msg = "the answer has no file summary"
        raise ValueError(msg)
    _, asked, cut = render(file)
    known = dict(zip(handles(asked), (symbol.key for symbol in asked), strict=True))
    symbols: dict[str, SymbolSummary] = {}
    dropped = 0
    for entry in answer.symbols:
        key, text = known.get(entry.key.strip()), _tidy(entry.summary)
        if key is None or key in symbols or not text:
            dropped += 1
            continue
        symbols[key] = SymbolSummary(key, text, terms(entry.domain_terms))
    return FileSummary(file.path, summary, symbols, PROMPT_VERSION, result.model, cut), dropped


# ---------------------------------------------------------------- the cost guard


async def estimate_cost(
    client: AnthropicClient,
    requests: Sequence[LLMRequest],
    calculator: CostCalculator,
    *,
    sample: int = COUNT_SAMPLE,
    batch: bool = True,
) -> CostEstimate:
    """What sending ``requests`` should cost: as a batch, or one call each when not ``batch``
    (full price). Input: ``count_tokens`` on every request, or on ``sample`` of them across the
    sizes and a straight-line fit (tokens by characters) for the rest. Output: the per-symbol
    budget."""
    if not requests:
        return CostEstimate(0, 0, 0, Decimal(0), 0)
    sizes = [len(r.prompt) for r in requests]
    by_size = sorted(range(len(requests)), key=lambda i: sizes[i])
    if len(requests) <= sample:
        chosen = by_size
    else:
        step = (len(requests) - 1) / (sample - 1)
        chosen = sorted({by_size[round(i * step)] for i in range(sample)})
    counts = await asyncio.gather(*(client.count_tokens(requests[i]) for i in chosen))
    counted = dict(zip(chosen, counts, strict=True))
    fit = _fit([(sizes[i], counted[i]) for i in chosen])
    inputs = [counted.get(i, fit(sizes[i])) for i in range(len(requests))]
    outputs = [r.max_tokens - THINKING_ROOM for r in requests]
    model = client.model_for(requests[0].tier)
    # Priced one request at a time: a request's size decides its rate (long-context pricing).
    # Prompt caching isn't counted, so this is on the high side.
    costs = [
        calculator.cost(PROVIDER, model, tokens_in=i, tokens_out=o, batch=batch)
        for i, o in zip(inputs, outputs, strict=True)
    ]
    if any(cost is None for cost in costs):
        msg = f"no price for {model}: the cost guard can't estimate"
        raise ValueError(msg)
    total = sum((cost for cost in costs if cost is not None), Decimal(0))
    return CostEstimate(len(requests), sum(inputs), sum(outputs), total, len(chosen))


def _fit(points: Sequence[tuple[int, int]]) -> Callable[[int], int]:
    """tokens = a + b * characters by least squares (a ratio when the sizes don't differ)."""
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    if spread == 0:
        ratio = mean_y / mean_x if mean_x else 0.0
        return lambda size: math.ceil(size * ratio)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
    intercept = mean_y - slope * mean_x
    return lambda size: max(1, math.ceil(intercept + slope * size))


async def lock_spending(db: AsyncSession, org_id: uuid.UUID) -> None:
    """Hold the org's cost-guard lock until ``db``'s transaction ends: another check-then-reserve
    for the org waits for this one to commit (and then sees its reservation)."""
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(COST_LOCK), func.hashtext(str(org_id))))
    )


async def pending_estimates(db: AsyncSession, org_id: uuid.UUID) -> Decimal:
    """The estimates of the org's batches whose cost isn't in the ledger yet: reserved, sent or
    ended, but not collected (and not failed)."""
    total = await db.scalar(
        select(func.coalesce(func.sum(LLMBatch.estimated_cost_usd), 0)).where(
            LLMBatch.org_id == org_id,
            LLMBatch.collected_at.is_(None),
            LLMBatch.status != LLMBatchStatus.FAILED,
        )
    )
    return Decimal(total or 0)


async def check_budget(
    db: AsyncSession,
    org_id: uuid.UUID,
    estimate: CostEstimate,
    cap_usd: Decimal,
    *,
    now: datetime | None = None,
) -> Decimal:
    """The org's spend this month, with what its uncollected batches are expected to cost; raises
    :class:`CostCapExceeded` if the estimate on top of it would pass ``cap_usd``. Takes the org's
    cost-guard lock: reserve in the same transaction (:func:`reserve_batch`)."""
    await lock_spending(db, org_id)
    spent = await spent_since(db, org_id, month_start(now)) + await pending_estimates(db, org_id)
    if spent + estimate.cost_usd > cap_usd:
        raise CostCapExceeded(estimate, spent, cap_usd)
    return spent


async def reserve_batch(
    db: AsyncSession,
    client: AnthropicClient,
    requests: Sequence[tuple[str, LLMRequest]],
    estimate: CostEstimate,
    *,
    org_id: uuid.UUID,
    cap_usd: Decimal,
    batch_cost_usd: Decimal | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> LLMBatch:
    """Check ``estimate`` against the cap and reserve the batch, under the org's lock (commit,
    then :func:`batches.send`). ``batch_cost_usd``: the batch's own share of the estimate, when
    the estimate covers more (the rollups after it); recorded on the row."""
    await check_budget(db, org_id, estimate, cap_usd)
    row = await batches.reserve(
        db, client, requests, org_id=org_id, kind=KIND, ref_type=ref_type, ref_id=ref_id
    )
    row.estimated_cost_usd = estimate.cost_usd if batch_cost_usd is None else batch_cost_usd
    await db.flush()
    return row


# ---------------------------------------------------------------- the run


async def summarize_files(
    sessions: Sessions,
    client: AnthropicClient,
    files: Sequence[FileInput],
    *,
    org_id: uuid.UUID,
    calculator: CostCalculator,
    cap_usd: Decimal,
    store: Store | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    poll_seconds: float = POLL_SECONDS,
    sleep: Sleep = asyncio.sleep,
) -> SummaryRun:
    """Summarize ``files`` in one batch: estimate, check the cap, send, wait, collect, retry
    what failed once. ``sessions`` opens a transaction scoped to ``org_id`` (committed when the
    block ends): the batch row is committed before the batch is sent. ``store`` keeps the
    summaries in the collecting transaction, and again after the retries.

    With ``ref_id``, a batch still open for ``ref_type``/``ref_id`` (sent by a run that
    stopped) is waited for and collected instead of a new one; one still being sent is first
    settled (:func:`batches.reconcile`), and only a send that failed is sent again."""
    requests = build_requests(files)
    if not requests:
        return SummaryRun()
    estimate: CostEstimate | None = None
    batch_id = await _open_batch(
        sessions, client, ref_type, ref_id, poll_seconds=poll_seconds, sleep=sleep
    )
    if batch_id is None:
        estimate = await estimate_cost(client, [request for _, request in requests], calculator)
        batch_id = await _send(
            sessions, client, requests, estimate, org_id=org_id, cap_usd=cap_usd,
            ref_type=ref_type, ref_id=ref_id,
        )  # fmt: skip
    else:
        log.info("summaries batch resumed", batch_id=str(batch_id))
    while True:
        async with sessions() as db:
            row = await _batch(db, batch_id)
            await batches.refresh(db, client, row)
            ended = row.status is LLMBatchStatus.ENDED
        if ended:
            break
        await sleep(poll_seconds)
    async with sessions() as db:
        run = await collect_summaries(
            db,
            client,
            await _batch(db, batch_id),
            files,
            calculator=calculator,
            org_id=org_id,
            store=store,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    run.estimate = estimate
    run.batch_id = batch_id
    retried = [file for file in files if file.path in run.skipped]
    await retry_failed(
        sessions,
        client,
        run,
        retried,
        calculator=calculator,
        org_id=org_id,
        cap_usd=cap_usd,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    if retried and store is not None:
        async with sessions() as db:
            await store(db, run)
    return run


async def _open_batch(
    sessions: Sessions,
    client: AnthropicClient,
    ref_type: str | None,
    ref_id: str | None,
    *,
    poll_seconds: float,
    sleep: Sleep,
) -> uuid.UUID | None:
    """The batch still open for the reference (its id), or None when a new one must be sent.
    A row still ``submitting`` is settled first: it may have reached the provider."""
    if ref_id is None:
        return None
    async with sessions() as db:
        found = await batches.uncollected(db, kind=KIND, ref_type=ref_type, ref_id=ref_id)
    if not found:
        return None
    batch_id = found[-1].id
    while True:
        async with sessions() as db:
            await batches.reconcile(db, client)
            status = (await _batch(db, batch_id)).status
        if status is LLMBatchStatus.FAILED:
            return None  # it never went out
        if status is not LLMBatchStatus.SUBMITTING:
            return batch_id
        await sleep(poll_seconds)


async def _send(
    sessions: Sessions,
    client: AnthropicClient,
    requests: Sequence[tuple[str, LLMRequest]],
    estimate: CostEstimate,
    *,
    org_id: uuid.UUID,
    cap_usd: Decimal,
    ref_type: str | None,
    ref_id: str | None,
) -> uuid.UUID:
    """Reserve within the cap, then send. A send the provider refused marks the row failed;
    one that may have gone out (no answer, overloaded) leaves it to be settled."""
    async with sessions() as db:
        row = await reserve_batch(
            db, client, requests, estimate, org_id=org_id, cap_usd=cap_usd,
            ref_type=ref_type, ref_id=ref_id,
        )  # fmt: skip
        batch_id = row.id
    try:
        async with sessions() as db:
            await batches.send(db, client, batch_id, requests)
    except LLMError as exc:
        if not exc.transient:
            async with sessions() as db:
                (await _batch(db, batch_id)).status = LLMBatchStatus.FAILED
        raise
    log.info("summaries batch sent", files=len(requests), estimate_usd=str(estimate.cost_usd))
    return batch_id


async def _batch(db: AsyncSession, batch_id: uuid.UUID) -> LLMBatch:
    row = await db.get(LLMBatch, batch_id)
    if row is None:  # pragma: no cover - the row was committed above
        msg = f"batch {batch_id} is gone"
        raise LookupError(msg)
    return row


async def collect_summaries(
    db: AsyncSession,
    client: AnthropicClient,
    batch: LLMBatch,
    files: Iterable[FileInput],
    *,
    calculator: CostCalculator,
    org_id: uuid.UUID,
    store: Store | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> SummaryRun:
    """Collect an ended summaries batch in ``db``'s transaction: the claim, the usage and what
    ``store`` keeps commit together, or none of them does (and the batch can be collected
    again). Files without a usable answer are in ``skipped``, ready for :func:`retry_failed`.
    Works after a restart: ``files`` are matched by path."""
    by_id = {file.custom_id: file for file in files}
    sink = usage_ledger(db, calculator, org_id=org_id, ref_type=ref_type, ref_id=ref_id)
    items = await batches.collect(db, client, batch, json_output=True, on_usage=sink)
    run = SummaryRun(batch_id=batch.id)
    for item in items:
        file = by_id.pop(item.custom_id, None)
        if file is None:
            log.warning("summaries batch answered an unknown request", custom_id=item.custom_id)
            continue
        _take(run, file, item)
    for file in by_id.values():
        run.skipped[file.path] = "no result in the batch"
    if store is not None:
        await store(db, run)
    return run


def _take(run: SummaryRun, file: FileInput, item: BatchItem) -> None:
    if item.outcome != "succeeded" or item.result is None:
        reason = item.error.message if item.error is not None else item.outcome
        run.skipped[file.path] = f"{item.outcome}: {reason}"
        return
    try:
        summary, dropped = parse_answer(file, item.result)
    except ValueError as exc:
        run.skipped[file.path] = str(exc)
        return
    run.summaries[file.path] = summary
    run.dropped_keys += dropped


async def retry_failed(
    sessions: Sessions,
    client: AnthropicClient,
    run: SummaryRun,
    files: Sequence[FileInput],
    *,
    calculator: CostCalculator,
    org_id: uuid.UUID,
    cap_usd: Decimal,
    ref_type: str | None = None,
    ref_id: str | None = None,
    concurrency: int = RETRY_CONCURRENCY,
) -> None:
    """Send each of ``files`` once more on its own (full price, at once), if the estimate
    passes the cost guard; a file that fails again stays in ``run.skipped``, with the reason
    logged. Calls made at once aren't reserved like a batch: the guard's lock is held for the
    check only."""
    if not files:
        return
    estimate = await estimate_cost(
        client, [build_request(file) for file in files], calculator, batch=False
    )
    try:
        async with sessions() as db:
            await check_budget(db, org_id, estimate, cap_usd)
    except CostCapExceeded as exc:
        for file in files:
            run.skipped[file.path] = "not retried: over the monthly cost cap"
        log.warning("summary retries over the cost cap", files=len(files), spent=str(exc.spent))
        return
    gate = asyncio.Semaphore(concurrency)
    billed: list[LLMUsage] = []

    async def bill(usage: LLMUsage) -> None:
        billed.append(usage)

    async def retry(file: FileInput) -> None:
        async with gate:
            try:
                result = await client.complete(build_request(file), on_usage=bill)
                summary, dropped = parse_answer(file, result)
            except (LLMError, ValueError) as exc:
                reason = exc.message if isinstance(exc, LLMError) else str(exc)
                run.skipped[file.path] = f"retried: {reason}"
                log.warning("file summary skipped", path=file.path, reason=reason)
                return
        run.skipped.pop(file.path, None)
        run.summaries[file.path] = summary
        run.dropped_keys += dropped

    run.retried += len(files)
    try:
        await asyncio.gather(*(retry(file) for file in files))
    finally:
        async with sessions() as db:
            sink = usage_ledger(db, calculator, org_id=org_id, ref_type=ref_type, ref_id=ref_id)
            for usage in billed:
                await sink(usage)


# ---------------------------------------------------------------- the artifact


def to_json(summaries: Mapping[str, FileSummary], skipped: Mapping[str, str]) -> bytes:
    """A snapshot's summaries (and the files skipped, with why) as its JSON artifact."""
    return json.dumps(
        {
            "files": [
                {
                    "path": summary.path,
                    "summary": summary.summary,
                    "prompt_version": summary.prompt_version,
                    "model": summary.model,
                    "truncated": summary.truncated,
                    "symbols": [
                        {"key": s.key, "summary": s.summary, "domain_terms": list(s.domain_terms)}
                        for s in summary.symbols.values()
                    ],
                }
                for summary in sorted(summaries.values(), key=lambda summary: summary.path)
            ],
            "skipped": dict(sorted(skipped.items())),
        },
        ensure_ascii=False,
    ).encode()


def from_json(data: bytes) -> tuple[dict[str, FileSummary], dict[str, str]]:
    """What :func:`to_json` wrote: the summaries by path, and the skipped files."""
    raw = json.loads(data)
    summaries = {
        file["path"]: FileSummary(
            file["path"],
            file["summary"],
            {
                s["key"]: SymbolSummary(s["key"], s["summary"], tuple(s["domain_terms"]))
                for s in file["symbols"]
            },
            file["prompt_version"],
            file["model"],
            file["truncated"],
        )
        for file in raw["files"]
    }
    return summaries, dict(raw["skipped"])
