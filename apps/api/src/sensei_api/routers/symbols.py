"""Symbols of an indexed snapshot: lookup by name (go to symbol, ⌘K) and the symbol under the
cursor (the code viewer).

``ref`` picks the snapshot as in browsing; the commit read is in ``X-Commit-Sha``. A snapshot
whose symbols aren't extracted yet answers ``409 not_indexed``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlalchemy import select

from sensei_api.deps import DbSession
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import COMMIT_HEADER, BadPathError, RefParam
from sensei_core.errors import ConflictError, InvalidError, NotFoundError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import RepoFile, Snapshot
from sensei_engine.snapshots import UnsafePathError, check_path
from sensei_engine.symbols.extract import SymbolKind
from sensei_engine.symbols.lookup import find_symbols, symbols_at

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["symbols"])

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
_COMMIT_HEADER_DOC = {
    COMMIT_HEADER: {
        "description": "The commit the response was read from.",
        "schema": {"type": "string"},
    }
}


class SymbolOut(BaseModel):
    symbol_key: str
    name: str
    qualified_name: str
    kind: SymbolKind
    path: str
    start_line: int
    end_line: int
    signature: str | None
    score: float


class SymbolsOut(BaseModel):
    query: str
    results: list[SymbolOut]


class SymbolAt(BaseModel):
    symbol_key: str
    name: str
    qualified_name: str
    kind: SymbolKind
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None


class SymbolsAtOut(BaseModel):
    path: str
    line: int
    symbols: list[SymbolAt]  # innermost first; the module last


def _require_symbols(snapshot: Snapshot) -> None:
    if snapshot.symbol_count is None:
        raise ConflictError(
            "Symbols appear once this commit's indexing has extracted them.", code="not_indexed"
        )


@router.get("/symbols", response_model=SymbolsOut, responses={200: {"headers": _COMMIT_HEADER_DOC}})
async def search_symbols(
    *,
    response: Response,
    repo: RepoDep,
    db: DbSession,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    kind: SymbolKind | None = None,
    ref: RefParam = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> SymbolsOut:
    """Symbols by name, best first: exact, prefix, qualified name, contains, then similar."""
    query = q.strip()
    if not query:
        raise InvalidError("Type something to search for.", code="invalid_query")
    snapshot = await resolve_snapshot(db, repo, ref)
    _require_symbols(snapshot)
    hits = await find_symbols(db, snapshot.id, query, kind=kind, limit=limit)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    results = [
        SymbolOut(
            symbol_key=h.symbol_key,
            name=h.name,
            qualified_name=h.qualified_name,
            kind=SymbolKind(h.kind),
            path=h.path,
            start_line=h.start_line,
            end_line=h.end_line,
            signature=h.signature,
            score=h.score,
        )
        for h in hits
    ]
    return SymbolsOut(query=query, results=results)


@router.get(
    "/symbols/at", response_model=SymbolsAtOut, responses={200: {"headers": _COMMIT_HEADER_DOC}}
)
async def symbol_at(
    *,
    response: Response,
    repo: RepoDep,
    db: DbSession,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
    line: Annotated[int, Query(ge=1)],
    ref: RefParam = None,
) -> SymbolsAtOut:
    """The definitions around a line of a file, innermost first."""
    try:
        relative = check_path(path)
    except UnsafePathError:
        raise BadPathError("That isn't a path inside the repository.") from None
    snapshot = await resolve_snapshot(db, repo, ref)
    _require_symbols(snapshot)
    file_id = await db.scalar(
        select(RepoFile.id).where(RepoFile.snapshot_id == snapshot.id, RepoFile.path == relative)
    )
    if file_id is None:
        raise NotFoundError("No such file in this snapshot.", code="path_not_found")
    nodes = await symbols_at(db, file_id, line)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    symbols = [
        SymbolAt(
            symbol_key=n.symbol_key,
            name=n.name,
            qualified_name=n.qualified_name,
            kind=SymbolKind(n.kind),
            start_line=n.start_line,
            end_line=n.end_line,
            signature=n.signature,
            docstring=n.docstring,
        )
        for n in nodes
    ]
    return SymbolsAtOut(path=relative, line=line, symbols=symbols)
