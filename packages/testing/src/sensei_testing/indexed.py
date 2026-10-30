"""A small repository indexed for real (files, symbols, chunks, the graph), for tests of what
reads an index: the agent's tools, its pre-retrieval."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import Repo, RepoSource, Snapshot
from sensei_core.orgs.service import OrgService
from sensei_engine.chunk.files import chunk_file
from sensei_engine.chunk.store import FileChunks, save_chunks
from sensei_engine.graph import python as python_graph
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.graph.stage import load_project, resolve_graph, write_graph
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.store import save_discovery
from sensei_engine.parse.parser import parse_file
from sensei_engine.parse.stage import parseable
from sensei_engine.symbols.extract import extract_symbols
from sensei_engine.symbols.store import FileSymbols, save_symbols
from sensei_testing import make_user

ROUTE = "backend/app/api/routes/items.py"
SERVICE = "backend/app/services/items.py"
ITEMS: Mapping[str, str] = {
    "backend/pyproject.toml": '[project]\nname = "app"\n',
    ROUTE: (
        "from app.services.items import create_item\n"
        "\n"
        "\n"
        "@router.post('/items/')\n"
        "def create_item_route(item_in):\n"
        '    """Create a new item."""\n'
        "    return create_item(item_in)\n"
    ),
    SERVICE: (
        "def create_item(item_in):\n"
        "    item = Item(title=item_in.title)\n"
        "    session.add(item)\n"
        "    return item\n"
    ),
    "backend/app/core/long.py": "".join(f"VALUE_{n} = '{'x' * 180}'\n" for n in range(300)),
    "README.md": "# Items\n\nCreate an item with POST /items/.\n",
}


async def index_repo(
    db: AsyncSession, root: Path, files: Mapping[str, str]
) -> tuple[Snapshot, GraphSnapshot]:
    """``files`` written under ``root`` and indexed as the pipeline indexes a snapshot:
    discovered, parsed into symbols and chunks, its Python linked into the graph (read back
    into memory)."""
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=11,
        owner="o",
        name="items",
        full_name="o/items",
        slug="items",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="main", commit_sha="b" * 40)
    db.add(snapshot)
    await db.flush()
    discovery = discover(root, max_files=100)
    await save_discovery(db, snapshot, discovery)
    file_ids = {f.path: f.id for f in discovery.files}
    symbols: list[FileSymbols] = []
    chunks: list[FileChunks] = []
    facts: dict[str, object] = {}
    for found in discovery.files:
        data = (root / found.path).read_bytes()
        parsed = found_symbols = None
        if parseable(found.language) and found.language is not None:
            parsed = parse_file(found.path, data, language=found.language)
            found_symbols = extract_symbols(parsed, found.language)
            symbols.append(FileSymbols(found.path, found.language, found_symbols))
            if found.path.endswith(".py") and (made := python_graph.extract(parsed)) is not None:
                facts[found.path] = made
        made_chunks = chunk_file(
            found.path, found.language, data, parsed=parsed, symbols=found_symbols
        )
        chunks.append(FileChunks(found.path, found.language, made_chunks))
    await save_symbols(db, snapshot, file_ids, symbols)
    await save_chunks(db, snapshot, file_ids, chunks)
    await db.flush()
    project = await load_project(db, snapshot.id, root)
    await write_graph(db, snapshot, resolve_graph(project, {"python": facts}))
    return snapshot, await GraphSnapshot.build(db, snapshot.id)
