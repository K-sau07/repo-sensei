"""The eval index's graph (3.6, 3.12): linked as the graph and links stages link it, so the
agent's traces in answer evals cross from a route to the code it runs, as they do in the app."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models.graph import EdgeType
from sensei_evals.graph import link_snapshot
from sensei_testing.indexed import index_repo

ROUTES = "backend/app/api/items.py"
APP: dict[str, str] = {
    "backend/pyproject.toml": '[project]\nname = "app"\n',
    "backend/app/__init__.py": "",
    "backend/app/api/__init__.py": "",
    ROUTES: (
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        "@router.post('/items/')\n"
        "def create_item(title: str) -> dict[str, str]:\n"
        "    return {'title': title}\n"
    ),
    "backend/app/main.py": (
        "from fastapi import FastAPI\n"
        "\n"
        "from app.api import items\n"
        "\n"
        "app = FastAPI()\n"
        "app.include_router(items.router, prefix='/api/v1')\n"
    ),
}


def _sessions(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def opened() -> AsyncGenerator[AsyncSession]:
        yield session

    return opened


@pytest.mark.integration
async def test_the_eval_graph_has_the_routes_endpoints_and_their_handlers(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, _ = await index_repo(db_session, tmp_path, APP)
    graph = await link_snapshot(_sessions(db_session), snapshot.id, tmp_path)
    endpoints = [n for n in graph.nodes if n.kind == "api_endpoint"]
    assert [n.name for n in endpoints] == ["POST /api/v1/items/"]
    hops, _ = graph.neighbors(endpoints[0].symbol_key, edge_types=[EdgeType.HANDLED_BY])
    assert [(h.node.path, h.node.name) for h in hops] == [(ROUTES, "create_item")]
