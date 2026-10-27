"""How much an edge is believed, from how it is known (engine design §11.2).

A compiler's or the running app's word is certain (1.0); an import followed to its module 0.9; a
receiver's type guessed from ``self.x``, an annotation or a constructor 0.7; a URL whose every
literal segment matched 0.5; a name that is unique in the repository 0.4; one of ``k`` same-named
candidates ``0.2 / k``. What a model inferred is **flagged**: kept, but left out of anything that
must be sure (blast radius) unless asked for. A path is believed as much as all its edges
together: the product.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import prod

from sensei_core.models import Provenance

CONFIDENCE: Mapping[Provenance, float] = {
    Provenance.SCIP: 1.0,
    Provenance.LSP: 1.0,
    Provenance.RUNTIME: 1.0,
    Provenance.CONTRACT: 1.0,
    Provenance.IMPORT_RESOLVED: 0.9,
    Provenance.RECEIVER_HEURISTIC: 0.7,
    Provenance.URL_LITERAL: 0.5,
    Provenance.NAME_UNIQUE: 0.4,
    Provenance.NAME_AMBIGUOUS: 0.2,  # divided by the candidates (confidence_of)
    Provenance.LLM_INFERRED: 0.3,
}
# Kept apart from what the extractors know: off by default wherever certainty matters.
FLAGGED: frozenset[Provenance] = frozenset({Provenance.LLM_INFERRED})


def confidence_of(provenance: Provenance, candidates: int = 1) -> float:
    """An edge's default confidence; ``candidates``: how many targets a name could mean (only
    ``name_ambiguous`` divides by it)."""
    if candidates < 1:
        msg = f"candidates is at least 1, not {candidates}"
        raise ValueError(msg)
    base = CONFIDENCE[provenance]
    return base / candidates if provenance is Provenance.NAME_AMBIGUOUS else base


def path_confidence(confidences: Iterable[float]) -> float:
    """A path's confidence: the product of its edges' (an empty path is certain)."""
    return prod(confidences, start=1.0)
