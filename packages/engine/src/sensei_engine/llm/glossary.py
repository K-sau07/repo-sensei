"""The glossary (2.8.4): each domain term the symbol summaries use, with the symbols that use it,
so search can turn a question's words into the code's names. Built from summaries
(:func:`build_glossary`) and written per snapshot (:func:`save_glossary`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import GlossaryTerm
from sensei_engine.llm.summaries import FileSummary


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    term: str  # lower case, single spaces
    symbol_ids: tuple[uuid.UUID, ...]  # in the order the summaries came
    weight: float  # how many symbols name the term


def normalize_term(term: str) -> str:
    return " ".join(term.casefold().split())


def build_glossary(
    summaries: Iterable[FileSummary], node_ids: Mapping[str, uuid.UUID]
) -> list[GlossaryEntry]:
    """Every term of every symbol summary, with the nodes (``node_ids``: symbol key -> node id)
    that name it; symbols without a node are left out. Heaviest first, then by term."""
    found: dict[str, dict[uuid.UUID, None]] = {}
    for summary in summaries:
        for key, symbol in summary.symbols.items():
            node_id = node_ids.get(key)
            if node_id is None:
                continue
            for term in symbol.domain_terms:
                found.setdefault(normalize_term(term), {})[node_id] = None
    entries = [
        GlossaryEntry(term, tuple(ids), float(len(ids))) for term, ids in found.items() if term
    ]
    return sorted(entries, key=lambda entry: (-entry.weight, entry.term))


async def save_glossary(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    entries: Sequence[GlossaryEntry],
) -> int:
    """Replace the snapshot's glossary with ``entries`` (the session must be scoped to
    ``org_id``). Returns how many terms were written."""
    await db.execute(delete(GlossaryTerm).where(GlossaryTerm.snapshot_id == snapshot_id))
    if entries:
        await db.execute(
            insert(GlossaryTerm),
            [
                {
                    "org_id": org_id,
                    "repo_id": repo_id,
                    "snapshot_id": snapshot_id,
                    "term": entry.term,
                    "symbol_ids": list(entry.symbol_ids),
                    "weight": entry.weight,
                }
                for entry in entries
            ],
        )
    return len(entries)
