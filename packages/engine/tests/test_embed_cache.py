"""The embedding cache: misses embedded once and stored, hits read back for free, per org (row
level security; the app role may only read and add rows); groups stored as they finish; and each
request's cost in the usage ledger."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence
from decimal import Decimal

import pytest
from asyncpg.exceptions import CheckViolationError, InsufficientPrivilegeError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.db.types import half_precision
from sensei_core.mail import MemoryMailer
from sensei_core.models import EmbeddingCache, UsageEvent, UsageKind
from sensei_core.orgs.service import OrgService
from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator
from sensei_engine.embed import cache
from sensei_engine.embed.cache import embed_documents, lookup
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


async def _org(db: AsyncSession, slug: str = "acme") -> uuid.UUID:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name=slug, slug=f"{slug}-{owner.github_id}"
    )
    await set_org_context(db, access.org.id)
    return access.org.id


class FakeProvider:
    """Embeds each text as [length, 0.1, 0.2] and bills a token per character; fails on any
    text in ``fail_on``."""

    def __init__(
        self, model: str = "fake-code-1", dims: int = 3, fail_on: frozenset[str] = frozenset()
    ) -> None:
        self._model = model
        self._dims = dims
        self.fail_on = fail_on
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
        if self.fail_on.intersection(texts):
            raise RuntimeError("provider down")
        if on_usage is not None:
            tokens = sum(len(t) for t in texts)
            await on_usage(EmbeddingUsage(self.provider, self.model, len(texts), tokens))
        return [[float(len(t)), 0.1, 0.2][: self._dims] for t in texts]


async def test_misses_are_embedded_once_and_hits_cost_nothing(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    provider = FakeProvider()
    items = [(_hash("def a(): pass"), "def a(): pass"), (_hash("x = 1"), "x = 1")]
    items.append(items[0])  # the same chunk twice: embedded once
    billed: list[EmbeddingUsage] = []

    async def sink(usage: EmbeddingUsage) -> None:
        billed.append(usage)

    first = await embed_documents(db_session, provider, items, org_id=org_id, on_usage=sink)
    assert (first.hits, first.embedded) == (0, 2)
    assert provider.calls == [["def a(): pass", "x = 1"]]
    assert first.vectors[_hash("x = 1")] == half_precision([5.0, 0.1, 0.2])
    assert [usage.tokens for usage in billed] == [18]

    # The same chunks again (another snapshot, another repository): all hits, nothing billed.
    again = await embed_documents(db_session, provider, items, org_id=org_id, on_usage=sink)
    assert (again.hits, again.embedded) == (2, 0)
    assert len(provider.calls) == 1
    assert len(billed) == 1
    assert again.vectors == first.vectors  # the same vector, embedded or read back

    # Another model (or size) is another cache entry.
    other = FakeProvider(model="fake-code-2")
    third = await embed_documents(db_session, other, items[:1], org_id=org_id)
    assert (third.hits, third.embedded) == (0, 1)
    smaller = FakeProvider(dims=2)
    assert (await embed_documents(db_session, smaller, items[:1], org_id=org_id)).embedded == 1
    rows = await db_session.scalar(select(func.count()).select_from(EmbeddingCache))
    assert rows == 4
    hashes = [_hash("x = 1"), _hash("nope")]
    assert list(await lookup(db_session, org_id, model_key(provider), 3, hashes)) == [hashes[0]]


async def test_orgs_never_share_vectors(db_session: AsyncSession) -> None:
    acme = await _org(db_session, "acme")
    item = [(_hash("secret = 42"), "secret = 42")]
    await embed_documents(db_session, FakeProvider(), item, org_id=acme)

    globex = await _org(db_session, "globex")  # the session is now scoped to globex
    # Its own lookup misses (the same chunk is embedded and billed again for it) ...
    provider = FakeProvider()
    result = await embed_documents(db_session, provider, item, org_id=globex)
    assert (result.hits, result.embedded, len(provider.calls)) == (0, 1, 1)
    # ... and row-level security hides acme's row even when asked for by org.
    assert await lookup(db_session, acme, model_key(provider), 3, [item[0][0]]) == {}
    orgs = await db_session.scalars(select(EmbeddingCache.org_id))
    assert set(orgs) == {globex}
    with pytest.raises(DBAPIError):  # and it can't write into acme
        async with db_session.begin_nested():
            await cache.store(db_session, acme, "fake/m", 3, {_hash("y"): [1.0, 2.0, 3.0]})


async def test_a_failure_part_way_keeps_the_groups_already_stored(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = await _org(db_session)
    monkeypatch.setattr(cache, "EMBED_GROUP", 2)
    texts = ["a", "bb", "ccc", "dddd", "boom"]
    provider = FakeProvider(fail_on=frozenset({"boom"}))
    checkpoints: list[int] = []
    billed: list[EmbeddingUsage] = []

    async def sink(usage: EmbeddingUsage) -> None:
        billed.append(usage)

    async def checkpoint() -> None:  # a caller would commit here
        count = await db_session.scalar(select(func.count()).select_from(EmbeddingCache))
        checkpoints.append(count or 0)

    with pytest.raises(RuntimeError, match="provider down"):
        await embed_documents(
            db_session,
            provider,
            [(_hash(t), t) for t in texts],
            org_id=org_id,
            on_usage=sink,
            checkpoint=checkpoint,
        )

    assert checkpoints == [2, 4]  # each group stored before the next was sent
    assert [u.texts for u in billed] == [2, 2]
    found = await lookup(db_session, org_id, "fake/fake-code-1", 3, [_hash(t) for t in texts])
    assert len(found) == 4
    # The next run only embeds what is left.
    provider = FakeProvider()
    again = await embed_documents(
        db_session, provider, [(_hash(t), t) for t in texts], org_id=org_id
    )
    assert (again.hits, again.embedded, provider.calls) == (4, 1, [["boom"]])


async def test_content_hashes_must_be_sha256(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        await embed_documents(db_session, FakeProvider(), [(b"short", "x")], org_id=uuid.uuid4())


async def test_the_app_role_may_add_but_never_change_rows(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    await embed_documents(db_session, FakeProvider(), [(_hash("y"), "y")], org_id=org_id)
    for statement in (update(EmbeddingCache).values(dims=3), text("DELETE FROM embedding_cache")):
        with pytest.raises(DBAPIError) as raised:
            async with db_session.begin_nested():
                await db_session.execute(statement)
        assert isinstance(_cause(raised.value), InsufficientPrivilegeError)


async def test_a_vector_must_have_its_rows_size(owner_session: AsyncSession) -> None:
    org_id = await _org(owner_session)
    with pytest.raises(DBAPIError) as raised:
        await owner_session.execute(
            text(
                "INSERT INTO embedding_cache (org_id, content_hash, model, dims, embedding) "
                "VALUES (:org, :h, 'fake/m', 3, '[1,2]')"
            ),
            {"org": org_id, "h": _hash("z")},
        )
    assert isinstance(_cause(raised.value), CheckViolationError)


async def test_vectors_are_rounded_to_half_on_both_ways(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    # Values whose half-precision rounding pgvector could do differently from Python: the column
    # type sends them already rounded, so they are stored and read back exactly.
    vector = [0.1, 1 / 3, 2.0009765625, -65504.0, 6.0e-8, 1e-12]
    key = _hash("v")
    await cache.store(db_session, org_id, "fake/m", 6, {key: vector})
    found = await lookup(db_session, org_id, "fake/m", 6, [key])
    assert found[key] == half_precision(vector)
    stored = await db_session.scalar(text("SELECT embedding::text FROM embedding_cache"))
    assert stored is not None
    # pgvector prints the shortest digits that read back as the same half-precision values.
    assert half_precision([float(v) for v in str(stored)[1:-1].split(",")]) == half_precision(
        vector
    )


def test_half_precision_refuses_what_it_cannot_hold() -> None:
    for value in (65520.0, -1e6, math.inf, math.nan):
        with pytest.raises(ValueError, match="half precision"):
            half_precision([value])
    assert half_precision([65504.0]) == [65504.0]


async def test_each_request_is_priced_in_the_usage_ledger(db_session: AsyncSession) -> None:
    org_id = await _org(db_session)
    sink = usage_ledger(
        db_session,
        CostCalculator(DEFAULT_PRICE_TABLE),
        org_id=org_id,
        ref_type="index_job",
        ref_id="job-1",
    )
    await sink(EmbeddingUsage("voyage", "voyage-code-4", texts=1000, tokens=1_000_000))
    await sink(EmbeddingUsage("voyage", "voyage-code-4", texts=8, tokens=4_200))
    rows = (await db_session.scalars(select(UsageEvent).where(UsageEvent.org_id == org_id))).all()
    assert [(r.kind, r.provider, r.model, r.tokens_in) for r in rows] == [
        (UsageKind.EMBEDDING, "voyage", "voyage-code-4", 1_000_000),
        (UsageKind.EMBEDDING, "voyage", "voyage-code-4", 4_200),
    ]
    # voyage-code-4 lists at $0.12 per million tokens.
    assert [r.cost_usd for r in rows] == [Decimal("0.120000"), Decimal("0.000504")]
    assert {(r.ref_type, r.ref_id) for r in rows} == {("index_job", "job-1")}
