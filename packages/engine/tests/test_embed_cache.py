"""The embedding cache: misses embedded once and stored, hits read back for free, through the app
role (which may only read and add rows); and each request's cost in the usage ledger."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal

import pytest
from asyncpg.exceptions import CheckViolationError, InsufficientPrivilegeError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import EmbeddingCache, UsageEvent, UsageKind
from sensei_core.orgs.service import OrgService
from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator
from sensei_engine.embed.cache import embed_documents, half_precision, lookup
from sensei_engine.embed.provider import (
    EmbeddingUsage,
    InputType,
    UsageSink,
    model_key,
    usage_ledger,
)
from sensei_testing import make_user

pytestmark = pytest.mark.integration


def _cause(error: DBAPIError) -> BaseException | None:
    """The driver's own exception under SQLAlchemy's wrapper."""
    return error.orig.__cause__ if error.orig is not None else None


def _hash(text_: str) -> bytes:
    return hashlib.sha256(text_.encode()).digest()


class FakeProvider:
    """Embeds each text as [length, 0.1, 0.2] and bills a token per character."""

    def __init__(self, model: str = "fake-code-1", dims: int = 3) -> None:
        self._model = model
        self._dims = dims
        self.calls: list[list[str]] = []

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return self._dims

    async def embed(
        self, texts: Sequence[str], input_type: InputType, *, on_usage: UsageSink | None = None
    ) -> list[list[float]]:
        assert input_type is InputType.DOCUMENT
        self.calls.append(list(texts))
        if on_usage is not None:
            tokens = sum(len(t) for t in texts)
            await on_usage(EmbeddingUsage(self.provider, self.model, len(texts), tokens))
        return [[float(len(t)), 0.1, 0.2][: self._dims] for t in texts]


async def test_misses_are_embedded_once_and_hits_cost_nothing(db_session: AsyncSession) -> None:
    provider = FakeProvider()
    items = [(_hash("def a(): pass"), "def a(): pass"), (_hash("x = 1"), "x = 1")]
    items.append(items[0])  # the same chunk twice: embedded once
    billed: list[EmbeddingUsage] = []

    async def sink(usage: EmbeddingUsage) -> None:
        billed.append(usage)

    first = await embed_documents(db_session, provider, items, on_usage=sink)
    assert (first.hits, first.embedded) == (0, 2)
    assert provider.calls == [["def a(): pass", "x = 1"]]
    assert first.vectors[_hash("x = 1")] == half_precision([5.0, 0.1, 0.2])
    assert [usage.tokens for usage in billed] == [18]

    # The same chunks again (another snapshot, another repository): all hits, nothing billed.
    again = await embed_documents(db_session, provider, items, on_usage=sink)
    assert (again.hits, again.embedded) == (2, 0)
    assert len(provider.calls) == 1
    assert len(billed) == 1
    assert again.vectors == first.vectors  # the same vector, embedded or read back

    # Another model (or size) is another cache entry.
    other = FakeProvider(model="fake-code-2")
    third = await embed_documents(db_session, other, items[:1])
    assert (third.hits, third.embedded) == (0, 1)
    smaller = FakeProvider(dims=2)
    assert (await embed_documents(db_session, smaller, items[:1])).embedded == 1
    rows = await db_session.scalar(select(func.count()).select_from(EmbeddingCache))
    assert rows == 4
    keys = await lookup(db_session, model_key(provider), 3, [_hash("x = 1"), _hash("nope")])
    assert list(keys) == [_hash("x = 1")]


async def test_content_hashes_must_be_sha256(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        await embed_documents(db_session, FakeProvider(), [(b"short", "x")])


async def test_the_app_role_may_add_but_never_change_rows(db_session: AsyncSession) -> None:
    await embed_documents(db_session, FakeProvider(), [(_hash("y"), "y")])
    for statement in (update(EmbeddingCache).values(dims=3), text("DELETE FROM embedding_cache")):
        with pytest.raises(DBAPIError) as raised:
            async with db_session.begin_nested():
                await db_session.execute(statement)
        assert isinstance(_cause(raised.value), InsufficientPrivilegeError)


async def test_a_vector_must_have_its_rows_size(owner_session: AsyncSession) -> None:
    with pytest.raises(DBAPIError) as raised:
        await owner_session.execute(
            text(
                "INSERT INTO embedding_cache (content_hash, model, dims, embedding) "
                "VALUES (:h, 'fake/m', 3, '[1,2]')"
            ),
            {"h": _hash("z")},
        )
    assert isinstance(_cause(raised.value), CheckViolationError)


async def test_each_request_is_priced_in_the_usage_ledger(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db_session, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    await set_org_context(db_session, access.org.id)
    sink = usage_ledger(
        db_session,
        CostCalculator(DEFAULT_PRICE_TABLE),
        org_id=access.org.id,
        ref_type="index_job",
        ref_id="job-1",
    )
    await sink(EmbeddingUsage("voyage", "voyage-code-4", texts=1000, tokens=1_000_000))
    await sink(EmbeddingUsage("voyage", "voyage-code-4", texts=8, tokens=4_200))
    rows = (
        await db_session.scalars(select(UsageEvent).where(UsageEvent.org_id == access.org.id))
    ).all()
    assert [(r.kind, r.provider, r.model, r.tokens_in) for r in rows] == [
        (UsageKind.EMBEDDING, "voyage", "voyage-code-4", 1_000_000),
        (UsageKind.EMBEDDING, "voyage", "voyage-code-4", 4_200),
    ]
    # voyage-code-4 lists at $0.12 per million tokens.
    assert [r.cost_usd for r in rows] == [Decimal("0.120000"), Decimal("0.000504")]
    assert {(r.ref_type, r.ref_id) for r in rows} == {("index_job", "job-1")}
