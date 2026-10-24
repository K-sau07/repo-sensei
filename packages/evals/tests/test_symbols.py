"""The symbol retriever: a checkout indexed as the indexer would, its symbols looked up by the
names a golden query asks about."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import CodeNode, Org, Repo, Snapshot
from sensei_evals.retrieval import RepoRef
from sensei_evals.runner import run_suite
from sensei_evals.schema import GoldenItem, GoldTarget, Layer, QueryClass, Suite
from sensei_evals.symbols import (
    EVAL_ORG,
    SymbolRetriever,
    eval_database_url,
    index_checkout,
    lookup_names,
)

SHA = "c" * 40
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
    "app/service.py": (
        "def outer():\n    def make_item_later():\n        pass\n    return make_item_later\n"
    ),
    "tests/test_items.py": "def make_item():\n    pass\n",
    "web/src/useItem.ts": "export function useItem() {\n  return 1;\n}\n",
    "README.md": "# Items\n",
}


@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("where is `create_token` defined?", ["create_token"]),
        ("show me the `ItemsService` class and `ItemsService`", ["ItemsService"]),
        ("useAuth", ["useAuth"]),
        ("where is the Graph class defined?", ["Graph"]),
        ("Vertex class", ["Vertex"]),
        ("get_current_user", ["get_current_user"]),
        ("Settings.emails_enabled", ["Settings.emails_enabled"]),
        ("where is it?", []),
    ],
)
def test_lookup_names(query: str, names: list[str]) -> None:
    assert lookup_names(query) == names


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
async def test_checkout_is_indexed_once_per_run(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    repo = RepoRef(id="tiny", sha=SHA, checkout=_tree(tmp_path))
    first = await index_checkout(system_session, repo, tmp_path)
    # 4 modules; Item, save, make_item; outer, make_item_later; the test's make_item; useItem.
    assert first.symbol_count == 11
    again = await index_checkout(system_session, repo, tmp_path)
    org = await system_session.scalar(select(Org).where(Org.slug == EVAL_ORG))
    assert org is not None
    repos = (await system_session.scalars(select(Repo).where(Repo.org_id == org.id))).all()
    assert [row.slug for row in repos] == ["tiny"]
    assert repos[0].github_repo_id < 0  # never a real GitHub id
    of_repo = select(Snapshot.id).where(Snapshot.repo_id == repos[0].id)
    snapshots = (await system_session.scalars(of_repo)).all()
    assert snapshots == [again.id]  # the earlier eval snapshot is gone, its nodes with it
    nodes = await system_session.scalar(
        select(func.count()).select_from(CodeNode).where(CodeNode.snapshot_id == first.id)
    )
    assert nodes == 0


@pytest.mark.integration
async def test_symbol_retriever_finds_the_definition(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    repo = RepoRef(id="tiny", sha=SHA, checkout=_tree(tmp_path))
    closed: list[bool] = []

    async def close() -> None:
        closed.append(True)

    analyzed: list[bool] = []

    async def analyze() -> None:
        analyzed.append(True)

    retriever = SymbolRetriever(
        _sessions(system_session), checkout=tmp_path, close=close, analyze=analyze
    )
    await retriever.prepare(repo)
    await retriever.prepare(repo)  # once
    assert retriever.symbol_count == 11
    assert analyzed == [True]  # fresh statistics once the index is loaded

    hits = await retriever.search("where is `make_item` defined?", repo, 10)
    # The exact name first, the test's copy ranked below it, the nested longer name after both.
    assert [(h.path, h.lines, h.symbol) for h in hits[:3]] == [
        ("app/models.py", (6, 7), "make_item"),
        ("tests/test_items.py", (1, 2), "make_item"),
        ("app/service.py", (2, 3), "outer.make_item_later"),
    ]
    assert hits[0].score > hits[1].score > hits[2].score
    top = await retriever.search("Item class", repo, 1)
    assert [(h.path, h.symbol) for h in top] == [("app/models.py", "Item")]
    hook = await retriever.search("useItem", repo, 5)
    assert [(h.path, h.symbol) for h in hook[:2]] == [  # the hook, then its module
        ("web/src/useItem.ts", "useItem"),
        ("web/src/useItem.ts", "web.src.useItem"),
    ]
    assert await retriever.search("where is it?", repo, 5) == []  # nothing to look up: abstain

    items = [
        GoldenItem.model_validate(
            {
                "id": "tiny-sym-01",
                "repo": "tiny",
                "sha": SHA,
                "query": "where is `make_item` defined?",
                "class": QueryClass.SYMBOL.value,
                "layers": [Layer.DB.value],
                "gold": [
                    GoldTarget(
                        path="app/models.py", lines=(6, 7), symbol="make_item", layer=Layer.DB
                    )
                ],
            }
        )
    ]
    record = await run_suite(Suite(name="tiny", version=0, items=items), items, retriever, repo)
    assert record.retriever == "symbols"
    assert record.metrics["overall"]["recall_symbol@10"] == 1.0
    assert record.metrics["overall"]["mrr_symbol@10"] == 1.0
    assert closed == [True]  # the runner closes the retriever when it is done


def test_the_eval_index_never_goes_into_the_apps_database() -> None:
    url = "postgresql+asyncpg://sensei_system:pw@localhost:55432/sensei"
    assert eval_database_url(url).endswith("@localhost:55432/sensei_eval")
    assert eval_database_url(url, "sensei_eval_2").endswith("/sensei_eval_2")
    # Only names that say they are eval databases: it gets dropped and recreated.
    for name in (
        "sensei",
        "scratch",
        "sensei_test",
        "postgres",
        'sensei_eval"; DROP',
        "SENSEI_EVAL",
    ):
        with pytest.raises(ValueError, match="starts with 'sensei_eval'"):
            eval_database_url(url, name)
    # Not the app's database, nor the test database, even under an eval-looking name.
    with pytest.raises(ValueError, match="needs its own database"):
        eval_database_url(url.replace("/sensei", "/sensei_eval"), "sensei_eval")
    with pytest.raises(ValueError, match="needs its own database"):
        eval_database_url(url, "sensei_eval_t", reserved=["sensei_eval_t"])
    # A URL with no database can't say which one is the app's.
    with pytest.raises(ValueError, match="names no database"):
        eval_database_url("postgresql+asyncpg://sensei_system:pw@localhost:55432", "sensei_eval")


def test_symbol_retriever_needs_a_snapshot_or_checkout() -> None:
    with pytest.raises(ValueError, match="needs a snapshot or a checkout"):
        SymbolRetriever(_sessions(None))  # type: ignore[arg-type]
