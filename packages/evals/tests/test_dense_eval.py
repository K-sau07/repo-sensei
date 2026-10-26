"""The semantic retriever: a checkout indexed and embedded as the indexer would (through the
org's embedding cache), the question embedded as a query."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_engine.embed.provider import EmbeddingUsage, InputType, UsageSink
from sensei_evals.dense import SemanticRetriever, checkout_semantic
from sensei_evals.keyword import KeywordRetriever
from sensei_evals.retrieval import RepoRef, RetrieverContext
from sensei_evals.schema import Suite

SHA = "e" * 40
DIMS = 1024
FILES = {
    "app/models.py": (
        "class Item:\n"
        "    def save(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def make_item():\n"
        "    return Item()\n"
    ),
    "app/billing/invoices.py": "def total_due(invoice):\n    return sum(invoice.lines)\n",
    "README.md": "# Items\n\nHow items are made and saved.\n",
}


def _tree(root: Path) -> Path:
    for path, content in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return root


def _sessions(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def sessions() -> AsyncGenerator[AsyncSession]:
        yield session

    return sessions


def _axis(index: int) -> list[float]:
    vector = [0.0] * DIMS
    vector[index] = 1.0
    return vector


class FakeProvider:
    """Embeds by topic: money (invoice, bill, owed) along one axis, anything else another."""

    provider = "voyage"
    model = "voyage-code-4"
    dims = DIMS

    def __init__(self) -> None:
        self.calls: list[tuple[int, InputType]] = []

    async def embed(
        self, texts: Sequence[str], input_type: InputType, *, on_usage: UsageSink | None = None
    ) -> list[list[float]]:
        self.calls.append((len(texts), input_type))
        if on_usage is not None:
            await on_usage(EmbeddingUsage(self.provider, self.model, len(texts), 10 * len(texts)))
        money = ("invoice", "bill", "owed")
        return [_axis(1 if any(w in t.lower() for w in money) else 2) for t in texts]


def _retriever(
    session: AsyncSession, provider: FakeProvider, checkout: Path, mode: str
) -> SemanticRetriever:
    sessions = _sessions(session)
    keyword = KeywordRetriever(sessions, checkout=checkout)
    return SemanticRetriever(sessions, provider, keyword, mode=mode)


@pytest.mark.integration
async def test_dense_finds_by_meaning_and_a_second_index_is_all_cache(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    repo = RepoRef(id="tiny-dense", sha=SHA, checkout=_tree(tmp_path))
    provider = FakeProvider()
    retriever = _retriever(system_session, provider, tmp_path, "dense")
    await retriever.prepare(repo)
    # Item; make_item; total_due; the README.
    assert (retriever.cached, retriever.embedded) == (0, 4)
    assert {input_type for _, input_type in provider.calls} == {InputType.DOCUMENT}

    hits = await retriever.search("how much is owed on a bill?", repo, 2)
    assert hits[0].path == "app/billing/invoices.py"  # no shared word: by meaning
    assert provider.calls[-1] == (1, InputType.QUERY)
    assert retriever.tokens == 50  # four documents, one query

    again = _retriever(system_session, provider, tmp_path, "dense")
    sent = len(provider.calls)
    await again.prepare(repo)
    assert (again.cached, again.embedded, again.tokens) == (4, 0, 0)
    assert len(provider.calls) == sent  # nothing embedded


def test_the_factory_needs_a_checkout_and_a_voyage_key(tmp_path: Path) -> None:
    suite = Suite.model_construct(items=[])

    def context(checkout: Path | None) -> RetrieverContext:
        return RetrieverContext(suite=suite, repo=RepoRef("x", SHA, checkout))

    with pytest.raises(ValueError, match="needs a checkout"):
        checkout_semantic(context(None), "dense")
    with pytest.raises(ValueError, match="needs a Voyage API key"):  # tests have none
        checkout_semantic(context(tmp_path), "dense")
    with pytest.raises(ValueError, match="mode must be one of dense"):
        SemanticRetriever(None, FakeProvider(), None, mode="sparse")  # type: ignore[arg-type]
