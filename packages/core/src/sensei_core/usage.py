"""The usage ledger: every billable unit of work, priced when it happens.

Prices live in a versioned table (built in, or a JSON file named by ``SENSEI_PRICING_TABLE_PATH``).
Each ledger row stores the table version it was priced with, so past costs stay explainable after
prices change. Costs are list prices; provider free tiers are not subtracted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import PricingSettings
from sensei_core.db.base import new_id
from sensei_core.logging import get_logger
from sensei_core.models import UsageEvent, UsageKind

log = get_logger(__name__)

MILLION = Decimal(1_000_000)
CENT_FRACTION = Decimal("0.000001")  # matches cost_usd NUMERIC(12, 6)
ZERO = Decimal(0)


class Rates(BaseModel):
    """USD per million tokens, plus a flat price per unit for metered time."""

    input: Decimal = ZERO
    output: Decimal = ZERO
    cache_read: Decimal = ZERO
    cache_write: Decimal = ZERO
    per_unit: Decimal = ZERO


class ModelPrice(Rates):
    # Some models charge more once a request's prompt passes a size; the whole request is then
    # priced at ``long_context`` rates.
    long_context_threshold: int | None = None
    long_context: Rates | None = None
    batch_multiplier: Decimal = Decimal(1)


class PriceTable(BaseModel):
    version: str = Field(min_length=1, max_length=32)
    prices: dict[str, ModelPrice]  # keyed "provider/model"


def _d(value: str) -> Decimal:
    return Decimal(value)


# Anthropic rates: models page as of the version date (cache writes are the 5-minute rate).
# Voyage rates: Voyage pricing page. "sensei/*" rows are our own infrastructure cost estimates.
DEFAULT_PRICE_TABLE = PriceTable(
    version="2026-10-18",
    prices={
        "anthropic/claude-haiku-5-5": ModelPrice(
            input=_d("0.10"),
            output=_d("0.50"),
            cache_read=_d("0.01"),
            cache_write=_d("0.125"),
            long_context_threshold=100_000,
            long_context=Rates(
                input=_d("0.50"), output=_d("2.50"), cache_read=_d("0.05"), cache_write=_d("0.625")
            ),
            batch_multiplier=_d("0.5"),
        ),
        "anthropic/claude-sonnet-5-5": ModelPrice(
            input=_d("2"),
            output=_d("10"),
            cache_read=_d("0.10"),
            cache_write=_d("2.50"),
            batch_multiplier=_d("0.5"),
        ),
        "anthropic/claude-opus-5-5": ModelPrice(
            input=_d("4"),
            output=_d("20"),
            cache_read=_d("0.20"),
            cache_write=_d("5"),
            batch_multiplier=_d("0.5"),
        ),
        "anthropic/claude-fable-5-1": ModelPrice(
            input=_d("10"),
            output=_d("50"),
            cache_read=_d("0.50"),
            cache_write=_d("12.50"),
            batch_multiplier=_d("0.5"),
        ),
        "voyage/voyage-code-4": ModelPrice(input=_d("0.12"), batch_multiplier=_d("0.67")),
        "voyage/rerank-3": ModelPrice(input=_d("0.05")),
        "voyage/rerank-3-lite": ModelPrice(input=_d("0.02")),
        "sensei/index-worker": ModelPrice(per_unit=_d("0.002")),  # per index minute
        "sensei/sandbox": ModelPrice(per_unit=_d("0.00004")),  # per sandbox second
    },
)


class CostCalculator:
    def __init__(self, table: PriceTable) -> None:
        self.table = table

    @classmethod
    def from_settings(cls, settings: PricingSettings) -> CostCalculator:
        if settings.table_path is None:
            return cls(DEFAULT_PRICE_TABLE)
        return cls(PriceTable.model_validate_json(settings.table_path.read_bytes()))

    @property
    def version(self) -> str:
        return self.table.version

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        return self.table.prices.get(f"{provider}/{model}")

    def cost(
        self,
        provider: str,
        model: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        tokens_cache_read: int = 0,
        tokens_cache_write: int = 0,
        units: Decimal | int = 0,
        batch: bool = False,
    ) -> Decimal | None:
        """USD cost rounded to a millionth of a dollar, or None when the model has no price."""
        price = self.price_for(provider, model)
        if price is None:
            return None
        rates: Rates = price
        prompt_tokens = tokens_in + tokens_cache_read + tokens_cache_write
        threshold = price.long_context_threshold
        if price.long_context is not None and threshold is not None and prompt_tokens > threshold:
            rates = price.long_context
        total = (
            tokens_in * rates.input
            + tokens_out * rates.output
            + tokens_cache_read * rates.cache_read
            + tokens_cache_write * rates.cache_write
        ) / MILLION + Decimal(units) * rates.per_unit
        if batch:
            total *= price.batch_multiplier
        return total.quantize(CENT_FRACTION, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class UsageRecord:
    id: uuid.UUID
    cost_usd: Decimal
    priced: bool


async def record_usage(
    db: AsyncSession,
    calculator: CostCalculator,
    *,
    org_id: uuid.UUID,
    kind: UsageKind,
    provider: str,
    model: str,
    user_id: uuid.UUID | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    tokens_cache_read: int = 0,
    tokens_cache_write: int = 0,
    units: Decimal | int = 0,
    batch: bool = False,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> UsageRecord:
    """Append one ledger row (the transaction must be scoped to ``org_id``).

    Work that already happened is always recorded: a model missing from the price table is
    logged as an error and recorded at zero cost rather than dropped.
    """
    cost = calculator.cost(
        provider,
        model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_write=tokens_cache_write,
        units=units,
        batch=batch,
    )
    priced = cost is not None
    if cost is None:
        log.error("usage has no price", provider=provider, model=model, version=calculator.version)
        cost = ZERO
    event_id = new_id()
    await db.execute(
        insert(UsageEvent).values(
            id=event_id,
            org_id=org_id,
            user_id=user_id,
            kind=kind,
            provider=provider,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_write=tokens_cache_write,
            units=Decimal(units),
            cost_usd=cost,
            price_version=calculator.version,
            batch=batch,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    )
    return UsageRecord(id=event_id, cost_usd=cost, priced=priced)
