"""Finding symbols: by name (go to symbol, ⌘K) and by position (the symbol under the cursor).

:func:`find_symbols` matches, best first:

1. the exact name (with the same case scores higher), or the qualified name
   (``Settings.emails_enabled``);
2. a prefix of the name, scored by how much of the name it covers;
3. a qualified name ending in the query (``ItemsService.createItem``);
4. the name containing the query;
5. trigram similarity (typos: ``get_curent_user``).

The match score (0..1) is weighted by kind (definitions people look for, classes, functions,
components and hooks, over methods, modules and variables) and by the file: tests, generated and
vendored code rank lower.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Float, and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import CodeNode, RepoFile
from sensei_engine.symbols.extract import SymbolKind

KIND_WEIGHTS: dict[SymbolKind, float] = {
    SymbolKind.CLASS: 1.0,
    SymbolKind.COMPONENT: 1.0,
    SymbolKind.FUNCTION: 1.0,
    SymbolKind.HOOK: 1.0,
    SymbolKind.METHOD: 0.95,
    SymbolKind.MODULE: 0.9,
    SymbolKind.VARIABLE: 0.85,
}
TEST_WEIGHT = 0.85
GENERATED_WEIGHT = 0.9  # generated or vendored files
FUZZY_FLOOR = 0.3  # trigram similarity below this isn't a match


@dataclass(frozen=True, slots=True)
class SymbolHit:
    symbol_key: str
    name: str
    qualified_name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    signature: str | None
    score: float


def _number(value: float) -> ColumnElement[float]:
    return literal(value, Float)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def find_symbols(
    db: AsyncSession,
    snapshot_id: uuid.UUID,
    query: str,
    *,
    kind: SymbolKind | None = None,
    limit: int = 20,
) -> list[SymbolHit]:
    """The snapshot's symbols matching ``query`` (stripped, non-empty), best first."""
    lowered = query.lower()
    escaped = _like_escape(lowered)
    name = func.lower(CodeNode.name)
    qualified = func.lower(CodeNode.qualified_name)
    length: ColumnElement[float] = func.greatest(func.length(CodeNode.name), 1).cast(Float)
    coverage: ColumnElement[float] = (_number(len(query)) / length).cast(Float)
    similarity: ColumnElement[float] = func.similarity(name, lowered).cast(Float)
    match: ColumnElement[float] = case(
        (CodeNode.name == query, 1.0),
        (or_(name == lowered, qualified == lowered), 0.95),
        (name.like(escaped + "%", escape="\\"), _number(0.75) + coverage * 0.15),
        (qualified.like("%." + escaped, escape="\\"), 0.7),
        (name.like("%" + escaped + "%", escape="\\"), _number(0.5) + coverage * 0.2),
        else_=similarity * 0.5,
    )
    kind_weight: ColumnElement[float] = case(
        *((CodeNode.kind == k.value, _number(w)) for k, w in KIND_WEIGHTS.items()),
        else_=0.85,
    )
    file_weight: ColumnElement[float] = case(
        (RepoFile.role == "test", TEST_WEIGHT),
        (RepoFile.flags.overlap(["generated", "vendored"]), GENERATED_WEIGHT),
        else_=1.0,
    )
    score: ColumnElement[float] = (match * kind_weight * file_weight).cast(Float)
    conditions = [
        CodeNode.snapshot_id == snapshot_id,
        or_(
            name.like(escaped + "%", escape="\\"),
            qualified == lowered,
            qualified.like("%." + escaped, escape="\\"),
            CodeNode.name.ilike("%" + escaped + "%", escape="\\"),
            and_(CodeNode.name.op("%")(lowered), similarity >= FUZZY_FLOOR),
        ),
    ]
    if kind is not None:
        conditions.append(CodeNode.kind == kind.value)
    rows = await db.execute(
        select(
            CodeNode.symbol_key,
            CodeNode.name,
            CodeNode.qualified_name,
            CodeNode.kind,
            RepoFile.path,
            CodeNode.start_line,
            CodeNode.end_line,
            CodeNode.signature,
            score,
        )
        .join(RepoFile, RepoFile.id == CodeNode.file_id)
        .where(*conditions)
        .order_by(
            score.desc(),
            func.length(CodeNode.name),
            RepoFile.path,
            CodeNode.start_line,
        )
        .limit(limit)
    )
    return [
        SymbolHit(key, n, q, k, path, start, end, signature, round(float(s), 4))
        for key, n, q, k, path, start, end, signature, s in rows
    ]


async def symbols_at(db: AsyncSession, file_id: uuid.UUID, line: int) -> list[CodeNode]:
    """The symbols of a file whose lines include ``line``, innermost first (the module last)."""
    rows = await db.scalars(
        select(CodeNode)
        .where(
            CodeNode.file_id == file_id,
            CodeNode.start_line <= line,
            CodeNode.end_line >= line,
        )
        .order_by(
            (CodeNode.kind == SymbolKind.MODULE.value).asc(),
            (CodeNode.end_line - CodeNode.start_line).asc(),
            CodeNode.start_line.desc(),
        )
    )
    return list(rows)
