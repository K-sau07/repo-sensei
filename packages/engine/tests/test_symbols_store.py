"""Saving symbols as ``nodes`` (COPY through a staging table under row-level security) and finding
them: by name, ranked, and by position."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from asyncpg.exceptions import InsufficientPrivilegeError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import CodeNode, Repo, RepoFile, RepoSource, Snapshot
from sensei_core.orgs.service import OrgService
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.store import save_discovery
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import SymbolKind, extract_symbols
from sensei_engine.symbols.lookup import find_symbols, symbols_at
from sensei_engine.symbols.store import FileSymbols, save_symbols
from sensei_testing import make_user

pytestmark = pytest.mark.integration

FILES = {
    "app/models.py": (
        "class Item:\n"
        "    def save(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def make_item():\n"
        "    return Item()\n"
        "\n"
        "\n"
        "ITEM_LIMIT = 3\n"
    ),
    "app/items_service.py": (
        "class ItemsService:\n"
        "    def create_item(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "class Outer:\n"
        "    class Inner:\n"
        "        def run(self):\n"
        "            pass\n"
    ),
    "tests/test_items.py": "def make_item():\n    pass\n",
    "web/src/ItemCard.tsx": (
        "export const ItemCard = () => <div />;\n\nexport function useItem() {\n  return 1;\n}\n"
    ),
    "README.md": "# Items\n",
}


async def _snapshot(db: AsyncSession) -> Snapshot:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=7,
        owner="o",
        name="r",
        full_name="o/r",
        slug="r",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="main", commit_sha="a" * 40)
    db.add(snapshot)
    await db.flush()
    return snapshot


def _extracted(files: dict[str, str]) -> list[FileSymbols]:
    found: list[FileSymbols] = []
    for path, source in files.items():
        result = parse_file(path, source)
        if result.ok and result.language is not None:
            language = "tsx" if path.endswith(".tsx") else result.language
            found.append(FileSymbols(path, language, extract_symbols(result, language)))
    return found


async def _indexed(db: AsyncSession, root: Path) -> tuple[Snapshot, dict[str, uuid.UUID]]:
    for path, content in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    snapshot = await _snapshot(db)
    discovery = discover(root, max_files=100)
    await save_discovery(db, snapshot, discovery)
    file_ids = {f.path: f.id for f in discovery.files}
    await save_symbols(db, snapshot, file_ids, _extracted(FILES))
    return snapshot, file_ids


async def _keys(db: AsyncSession, snapshot: Snapshot) -> list[str]:
    rows = await db.scalars(select(CodeNode.symbol_key).where(CodeNode.snapshot_id == snapshot.id))
    return sorted(rows)


async def test_symbols_are_saved_with_their_file_and_snapshot(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, file_ids = await _indexed(db_session, tmp_path)
    assert await _keys(db_session, snapshot) == [
        "py app/items_service.py/",
        "py app/items_service.py/ItemsService#",
        "py app/items_service.py/ItemsService#create_item().",
        "py app/items_service.py/Outer#",
        "py app/items_service.py/Outer#Inner#",
        "py app/items_service.py/Outer#Inner#run().",
        "py app/models.py/",
        "py app/models.py/ITEM_LIMIT.",
        "py app/models.py/Item#",
        "py app/models.py/Item#save().",
        "py app/models.py/make_item().",
        "py tests/test_items.py/",
        "py tests/test_items.py/make_item().",
        "tsx web/src/ItemCard.tsx/",
        "tsx web/src/ItemCard.tsx/ItemCard().",
        "tsx web/src/ItemCard.tsx/useItem().",
    ]
    node = await db_session.scalar(
        select(CodeNode).where(CodeNode.symbol_key == "tsx web/src/ItemCard.tsx/ItemCard().")
    )
    assert node is not None
    assert (node.org_id, node.repo_id, node.snapshot_id) == (
        snapshot.org_id,
        snapshot.repo_id,
        snapshot.id,
    )
    assert node.file_id == node.owner_file_id == file_ids["web/src/ItemCard.tsx"]
    assert (node.kind, node.name, node.qualified_name, node.lang) == (
        "component",
        "ItemCard",
        "ItemCard",
        "tsx",
    )
    assert (node.start_line, node.end_line, node.start_col, node.end_col) == (1, 1, 0, 38)
    assert node.signature == "const ItemCard = () =>"
    assert node.attrs == {"exported": True}
    assert len(node.content_hash) == 32


async def test_saving_again_replaces_the_symbols(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot, file_ids = await _indexed(db_session, tmp_path)
    changed = {"app/models.py": "def make_item():\n    pass\n"}
    # An incremental save replaces only the files it is given.
    await save_symbols(db_session, snapshot, file_ids, _extracted(changed), replace_all=False)
    keys = await _keys(db_session, snapshot)
    assert [k for k in keys if k.startswith("py app/models.py")] == [
        "py app/models.py/",
        "py app/models.py/make_item().",
    ]
    assert "py app/items_service.py/ItemsService#" in keys
    # A full save replaces them all; files not in the snapshot are passed over.
    files = {**changed, "elsewhere.py": "x = 1\n"}
    assert await save_symbols(db_session, snapshot, file_ids, _extracted(files)) == 2
    assert await _keys(db_session, snapshot) == [
        "py app/models.py/",
        "py app/models.py/make_item().",
    ]


async def test_nul_characters_are_replaced(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot, file_ids = await _indexed(db_session, tmp_path)
    source = 'def make_item():\n    """Odd \\x00 text."""\n    return "\x00"\n'
    found = _extracted({"app/models.py": source})
    found[0].symbols[1].docstring = "a\x00b"
    found[0].symbols[1].attrs["note"] = "c\x00d"
    await save_symbols(db_session, snapshot, file_ids, found)
    node = await db_session.scalar(select(CodeNode).where(CodeNode.name == "make_item"))
    assert node is not None
    assert (node.docstring, node.attrs["note"]) == ("a�b", "c�d")


async def test_symbols_are_org_scoped(db_session: AsyncSession, tmp_path: Path) -> None:
    await _indexed(db_session, tmp_path)
    await set_org_context(db_session, uuid.uuid4())  # another org
    assert (await db_session.scalars(select(CodeNode))).all() == []


async def test_symbols_cannot_be_written_into_another_org(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, file_ids = await _indexed(db_session, tmp_path)
    await set_org_context(db_session, uuid.uuid4())
    with pytest.raises(InsufficientPrivilegeError, match="row-level security"):
        await save_symbols(db_session, snapshot, file_ids, _extracted(FILES), replace_all=False)


# ---------------------------------------------------------------------------- lookup


async def _found(
    db: AsyncSession, snapshot: Snapshot, query: str, kind: SymbolKind | None = None
) -> list[tuple[str, str]]:
    hits = await find_symbols(db, snapshot.id, query, kind=kind)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    return [(h.qualified_name, h.path) for h in hits]


async def test_an_exact_name_comes_first_then_prefixes(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, _ = await _indexed(db_session, tmp_path)
    hits = await find_symbols(db_session, snapshot.id, "Item")
    assert [(h.name, h.kind, h.score) for h in hits[:3]] == [
        ("Item", "class", 1.0),
        # Prefixes, scored by how much of the name the query covers.
        ("ItemCard", "component", 0.825),
        ("ItemsService", "class", 0.8),
    ]
    first = hits[0]
    assert (first.symbol_key, first.path, first.start_line, first.end_line) == (
        "py app/models.py/Item#",
        "app/models.py",
        1,
        3,
    )
    assert first.signature == "class Item"
    names = [h.name for h in hits]
    # Containing the query comes after the prefixes (case doesn't matter), shorter names first.
    assert names.index("ITEM_LIMIT") < names.index("useItem") < names.index("make_item")


async def test_the_same_name_in_a_test_ranks_lower(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, _ = await _indexed(db_session, tmp_path)
    found = await _found(db_session, snapshot, "make_item")
    assert found[:2] == [("make_item", "app/models.py"), ("make_item", "tests/test_items.py")]


async def test_qualified_names_match(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot, _ = await _indexed(db_session, tmp_path)
    found = await _found(db_session, snapshot, "itemsservice.create_item")
    assert found[0] == ("ItemsService.create_item", "app/items_service.py")
    # The end of a qualified name.
    found = await _found(db_session, snapshot, "Inner.run")
    assert found[0] == ("Outer.Inner.run", "app/items_service.py")


async def test_a_typo_still_finds_the_symbol(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot, _ = await _indexed(db_session, tmp_path)
    found = await _found(db_session, snapshot, "make_itme")
    assert found[:2] == [("make_item", "app/models.py"), ("make_item", "tests/test_items.py")]


async def test_the_kind_filter_and_like_wildcards(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot, _ = await _indexed(db_session, tmp_path)
    assert await _found(db_session, snapshot, "item", SymbolKind.HOOK) == [
        ("useItem", "web/src/ItemCard.tsx")
    ]
    # % and _ are themselves, not wildcards.
    assert await _found(db_session, snapshot, "%") == []
    hits = await find_symbols(db_session, snapshot.id, "_")
    assert hits
    assert all("_" in h.name for h in hits)


async def test_lookups_stay_in_their_snapshot(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot, _ = await _indexed(db_session, tmp_path)
    assert await find_symbols(db_session, uuid.uuid4(), "Item") == []
    assert await _found(db_session, snapshot, "Item", SymbolKind.METHOD) == [
        ("ItemsService.create_item", "app/items_service.py")  # contains "item"
    ]


async def test_the_symbols_at_a_line_innermost_first(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _, file_ids = await _indexed(db_session, tmp_path)
    service = file_ids["app/items_service.py"]
    nodes = await symbols_at(db_session, service, 9)
    assert [n.qualified_name for n in nodes] == [
        "Outer.Inner.run",
        "Outer.Inner",
        "Outer",
        "app.items_service",
    ]
    nodes = await symbols_at(db_session, service, 4)  # between definitions
    assert [n.kind for n in nodes] == ["module"]
    assert await symbols_at(db_session, service, 99) == []
    file_id = await db_session.scalar(select(RepoFile.id).where(RepoFile.path == "README.md"))
    assert file_id is not None
    assert await symbols_at(db_session, file_id, 1) == []
