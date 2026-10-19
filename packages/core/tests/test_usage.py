"""Usage ledger: cost math against the price table, and the append-only, org-scoped ledger."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import PricingSettings
from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import UsageEvent, UsageKind
from sensei_core.orgs.service import OrgService
from sensei_core.usage import (
    DEFAULT_PRICE_TABLE,
    CostCalculator,
    ModelPrice,
    PriceTable,
    record_usage,
)
from sensei_testing import make_user

MTOK = 1_000_000


@pytest.fixture
def calc() -> CostCalculator:
    return CostCalculator(DEFAULT_PRICE_TABLE)


# ------------------------------------------------------------------ cost math


def test_input_and_output_tokens(calc: CostCalculator) -> None:
    # Sonnet 5.5: $2 in, $10 out per million tokens.
    cost = calc.cost("anthropic", "claude-sonnet-5-5", tokens_in=MTOK, tokens_out=MTOK)
    assert cost == Decimal("12.000000")
    small = calc.cost("anthropic", "claude-sonnet-5-5", tokens_in=1_234, tokens_out=567)
    assert small == Decimal("0.008138")  # 1234 * 2e-6 + 567 * 10e-6 = 0.002468 + 0.00567


def test_cache_reads_and_writes_are_priced_separately(calc: CostCalculator) -> None:
    cost = calc.cost(
        "anthropic",
        "claude-opus-5-5",
        tokens_in=10_000,
        tokens_cache_read=80_000,
        tokens_cache_write=10_000,
        tokens_out=2_000,
    )
    # 10k * $4 + 80k * $0.20 + 10k * $5 + 2k * $20, per million.
    assert cost == Decimal("0.146000")


def test_long_prompts_switch_haiku_to_long_context_rates(calc: CostCalculator) -> None:
    at_limit = calc.cost("anthropic", "claude-haiku-5-5", tokens_in=100_000, tokens_out=1_000)
    assert at_limit == Decimal("0.010500")  # 100k * $0.10 + 1k * $0.50
    # Cached tokens count toward the prompt size; the whole request is then at the long rate.
    over = calc.cost(
        "anthropic",
        "claude-haiku-5-5",
        tokens_in=90_000,
        tokens_cache_read=20_000,
        tokens_out=1_000,
    )
    assert over == Decimal("0.048500")  # 90k * $0.50 + 20k * $0.05 + 1k * $2.50


def test_batch_discount(calc: CostCalculator) -> None:
    live = calc.cost("anthropic", "claude-haiku-5-5", tokens_in=MTOK // 10, tokens_out=MTOK // 10)
    batch = calc.cost(
        "anthropic", "claude-haiku-5-5", tokens_in=MTOK // 10, tokens_out=MTOK // 10, batch=True
    )
    assert (live, batch) == (Decimal("0.060000"), Decimal("0.030000"))
    embed = calc.cost("voyage", "voyage-code-4", tokens_in=MTOK, batch=True)
    assert embed == Decimal("0.080400")  # $0.12 with 33% off


def test_embeddings_reranking_and_metered_time(calc: CostCalculator) -> None:
    assert calc.cost("voyage", "voyage-code-4", tokens_in=MTOK) == Decimal("0.120000")
    assert calc.cost("voyage", "rerank-3", tokens_in=2 * MTOK) == Decimal("0.100000")
    assert calc.cost("sensei", "index-worker", units=Decimal("12.5")) == Decimal("0.025000")
    assert calc.cost("sensei", "sandbox", units=90) == Decimal("0.003600")


def test_rounds_half_up_to_a_millionth_of_a_dollar(calc: CostCalculator) -> None:
    # 3 tokens of Haiku input = $0.0000003, rounds to zero; 5 tokens = $0.0000005 rounds up.
    assert calc.cost("anthropic", "claude-haiku-5-5", tokens_in=3) == Decimal("0.000000")
    assert calc.cost("anthropic", "claude-haiku-5-5", tokens_in=5) == Decimal("0.000001")


def test_unknown_models_have_no_price(calc: CostCalculator) -> None:
    assert calc.cost("anthropic", "claude-unknown", tokens_in=10) is None
    assert calc.price_for("openai", "claude-sonnet-5-5") is None


# Published list prices, USD per million tokens: (input, output, cache read, 5-minute cache write).
# Kept separate from the table on purpose, so a typo in either one fails here.
ANTHROPIC_LIST_PRICES = {
    "claude-haiku-5-5": ("0.10", "0.50", "0.01", "0.125"),
    "claude-sonnet-5-5": ("2", "10", "0.20", "2.50"),
    "claude-opus-5-5": ("4", "20", "0.20", "5"),
    "claude-fable-5-1": ("10", "50", "0.25", "12.50"),
}


@pytest.mark.parametrize(("model", "prices"), ANTHROPIC_LIST_PRICES.items())
def test_anthropic_rates_match_list_prices(
    calc: CostCalculator, model: str, prices: tuple[str, str, str, str]
) -> None:
    price = calc.price_for("anthropic", model)
    assert price is not None
    expected = tuple(Decimal(p) for p in prices)
    assert (price.input, price.output, price.cache_read, price.cache_write) == expected
    assert price.batch_multiplier == Decimal("0.5")
    # 10k tokens of each kind (below Haiku's long-context threshold) cost 10k x the rate.
    costs = (
        calc.cost("anthropic", model, tokens_in=10_000),
        calc.cost("anthropic", model, tokens_out=10_000),
        calc.cost("anthropic", model, tokens_cache_read=10_000),
        calc.cost("anthropic", model, tokens_cache_write=10_000),
    )
    assert costs == tuple((rate * 10_000 / MTOK).quantize(Decimal("0.000001")) for rate in expected)


def test_every_default_price_is_non_negative() -> None:
    for key, price in DEFAULT_PRICE_TABLE.prices.items():
        rates = [price.input, price.output, price.cache_read, price.cache_write, price.per_unit]
        assert all(rate >= 0 for rate in rates), key
        assert Decimal(0) < price.batch_multiplier <= 1, key


def test_price_table_can_be_loaded_from_a_file(tmp_path: Path) -> None:
    table = PriceTable(version="test-1", prices={"acme/model-x": ModelPrice(input=Decimal("1"))})
    path = tmp_path / "prices.json"
    path.write_text(table.model_dump_json())
    calc = CostCalculator.from_settings(PricingSettings(table_path=path))
    assert calc.version == "test-1"
    assert calc.cost("acme", "model-x", tokens_in=MTOK) == Decimal("1.000000")
    assert CostCalculator.from_settings(PricingSettings()).version == DEFAULT_PRICE_TABLE.version


# ------------------------------------------------------------------ ledger


async def _org_id(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    return access.org.id, owner.id


@pytest.mark.integration
async def test_record_usage_writes_a_priced_row(
    db_session: AsyncSession, calc: CostCalculator
) -> None:
    org_id, user_id = await _org_id(db_session)
    record = await record_usage(
        db_session,
        calc,
        org_id=org_id,
        user_id=user_id,
        kind=UsageKind.LLM,
        provider="anthropic",
        model="claude-sonnet-5-5",
        tokens_in=1_234,
        tokens_out=567,
        ref_type="run",
        ref_id="run-1",
    )
    assert (record.cost_usd, record.priced) == (Decimal("0.008138"), True)
    row = await db_session.get(UsageEvent, record.id)
    assert row is not None
    assert (row.org_id, row.user_id, row.kind, row.model) == (
        org_id,
        user_id,
        UsageKind.LLM,
        "claude-sonnet-5-5",
    )
    assert (row.cost_usd, row.price_version, row.batch) == (
        Decimal("0.008138"),
        DEFAULT_PRICE_TABLE.version,
        False,
    )
    assert (row.ref_type, row.ref_id) == ("run", "run-1")


@pytest.mark.integration
async def test_unpriced_usage_is_still_recorded(
    db_session: AsyncSession, calc: CostCalculator
) -> None:
    org_id, _ = await _org_id(db_session)
    record = await record_usage(
        db_session,
        calc,
        org_id=org_id,
        kind=UsageKind.LLM,
        provider="anthropic",
        model="claude-unknown",
        tokens_in=10,
    )
    assert (record.cost_usd, record.priced) == (Decimal(0), False)
    assert await db_session.get(UsageEvent, record.id) is not None


@pytest.mark.integration
async def test_usage_is_org_scoped_and_append_only(
    db_session: AsyncSession, calc: CostCalculator
) -> None:
    first, _ = await _org_id(db_session)
    second, _ = await _org_id(db_session)  # the transaction is now scoped to `second`
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db_session.begin_nested():
            await record_usage(
                db_session,
                calc,
                org_id=first,
                kind=UsageKind.EMBEDDING,
                provider="voyage",
                model="voyage-code-4",
                tokens_in=100,
            )
    await record_usage(
        db_session,
        calc,
        org_id=second,
        kind=UsageKind.EMBEDDING,
        provider="voyage",
        model="voyage-code-4",
        tokens_in=100,
    )
    await set_org_context(db_session, first)
    assert (await db_session.scalars(select(UsageEvent))).all() == []
    for statement in ("UPDATE usage_events SET cost_usd = 0", "DELETE FROM usage_events"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db_session.begin_nested():
                await db_session.execute(text(statement))
