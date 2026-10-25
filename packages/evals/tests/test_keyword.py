"""The keyword retriever: a checkout indexed as the indexer would (files, symbols, chunks), the
question searched with BM25 on both legs."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_engine.search.bm25 import Leg
from sensei_evals.keyword import KeywordRetriever, checkout_keyword
from sensei_evals.retrieval import RepoRef, RetrieverContext
from sensei_evals.schema import Suite

SHA = "d" * 40
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
    "tests/test_items.py": "def test_make_item():\n    assert make_item()\n",
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


@pytest.mark.integration
async def test_keyword_retriever_indexes_once_and_ranks_by_bm25(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    repo = RepoRef(id="tiny-bm25", sha=SHA, checkout=_tree(tmp_path))
    analyzed: list[bool] = []

    async def analyze() -> None:
        analyzed.append(True)

    retriever = KeywordRetriever(_sessions(system_session), checkout=tmp_path, analyze=analyze)
    await retriever.prepare(repo)
    await retriever.prepare(repo)  # once
    # Item; make_item; total_due; the test; the README.
    assert retriever.chunk_count == 5
    assert analyzed == [True]
    assert retriever.index_seconds is not None

    hits = await retriever.search("make_item", repo, 10)
    assert [(h.path, h.lines, h.symbol) for h in hits[:2]] == [
        ("app/models.py", (6, 7), "py app/models.py/make_item()."),
        ("tests/test_items.py", (1, 2), "py tests/test_items.py/test_make_item()."),
    ]
    assert hits[0].score > hits[1].score
    billing = await retriever.search("where is the invoice total due computed?", repo, 3)
    assert billing[0].path == "app/billing/invoices.py"
    assert await retriever.search("xyzzyqq", repo, 5) == []  # no term matches: abstain


@pytest.mark.integration
async def test_one_leg_alone(system_session: AsyncSession, tmp_path: Path) -> None:
    repo = RepoRef(id="tiny-bm25", sha=SHA, checkout=_tree(tmp_path))
    path_only = KeywordRetriever(_sessions(system_session), checkout=tmp_path, legs=[Leg.PATH])
    hits = await path_only.search("billing invoices", repo, 3)
    assert hits[0].path == "app/billing/invoices.py"


def test_the_factory_checks_its_options(tmp_path: Path) -> None:
    suite = Suite.model_construct(items=[])

    def context(checkout: Path | None, **options: str) -> RetrieverContext:
        return RetrieverContext(
            suite=suite, repo=RepoRef("x", SHA, checkout), options=dict(options)
        )

    with pytest.raises(ValueError, match="needs a checkout"):
        checkout_keyword(context(None))
    with pytest.raises(ValueError, match="legs must be one of both, code, path"):
        checkout_keyword(context(tmp_path, legs="all"))
    with pytest.raises(ValueError, match="needs a snapshot or a checkout"):
        KeywordRetriever(_sessions(None))  # type: ignore[arg-type]
