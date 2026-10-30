"""Confidence and abstention (4.6): how sure an answer is, from its evidence, and when it is too
thin to give as an answer.

The score (0 to 1) weighs what the verification found (4.5):

* **support** (most of it): of the claims that cite, how many their lines show (a partial one
  counts half);
* **coverage**: of the claims, how many cite at all (one that doesn't is inferred);
* **the verifier**: whether the answer is whole (not run: neither way);
* **corroboration**: round 0's two passes agreeing, else the trace's own confidence (a flow
  question's), else neither way;

less for each invented name and each citation that pointed at nothing. ``high`` from
:data:`HIGH`, ``medium`` from :data:`MEDIUM`, else ``low``.

It **abstains** when nothing it cites is supported, when the score is under :data:`ABSTAIN`,
or when a flow question's trace stops at a gap in the graph: it says what it searched, where
the trail ended, and what to ask instead. The thresholds are a first guess, to be calibrated
on the golden set (4.14).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sensei_engine.agent.tools.base import Trail
from sensei_engine.agent.verify import Verification

HIGH = 0.75
MEDIUM = 0.5
ABSTAIN = 0.3
NEUTRAL = 0.5  # a part that didn't run counts neither way
INVENTED_PENALTY = 0.15  # each invented name, up to three
DROPPED_PENALTY = 0.05  # each citation that pointed at nothing, up to four
WEIGHTS = {"support": 0.45, "coverage": 0.2, "verifier": 0.2, "corroboration": 0.15}
Level = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class Breakdown:
    claims_total: int
    claims_cited: int
    claims_verified: int  # supported by the lines they cite
    claims_partial: int
    inferred: int  # claims that cite nothing
    unresolved_hops: int  # gaps in the trace
    agreement: float | None
    unknown_identifiers: int
    dropped_citations: int
    whole: bool | None  # the verifier's view (None: it didn't run)


@dataclass(frozen=True, slots=True)
class Confidence:
    level: Level
    score: float
    breakdown: Breakdown


@dataclass(frozen=True, slots=True)
class Abstention:
    searched: tuple[str, ...]
    trail_end: Trail | None  # where the flow's trace stopped, for a flow question
    reason: str
    suggestion: str


def confidence(verification: Verification | None, trail: Trail | None) -> Confidence:
    """The answer's confidence: its score, level and breakdown."""
    if verification is None:  # not verified: nothing it says has been checked
        empty = Breakdown(0, 0, 0, 0, 0, trail.gaps if trail else 0, None, 0, 0, None)
        return Confidence("low", 0.0, empty)
    checked = verification.claims
    cited = [c for c in checked if c.claim.cited]
    verified = sum(c.verdict == "supported" for c in cited)
    partial = sum(c.verdict == "partial" for c in cited)
    judged = [c for c in cited if c.verdict is not None]
    support = (verified + 0.5 * partial) / len(judged) if judged else (NEUTRAL if cited else 0.0)
    coverage = len(cited) / len(checked) if checked else 0.0
    whole = verification.whole.complete if verification.whole is not None else None
    verifier = NEUTRAL if whole is None else float(whole)
    corroboration = (
        verification.agreement
        if verification.agreement is not None
        else (trail.confidence if trail is not None else NEUTRAL)
    )
    score = (
        WEIGHTS["support"] * support
        + WEIGHTS["coverage"] * coverage
        + WEIGHTS["verifier"] * verifier
        + WEIGHTS["corroboration"] * corroboration
        - INVENTED_PENALTY * min(len(verification.unknown_identifiers), 3)
        - DROPPED_PENALTY * min(verification.dropped_citations, 4)
    )
    score = round(max(0.0, min(1.0, score)), 3)
    level: Level = "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"
    breakdown = Breakdown(
        len(checked),
        len(cited),
        verified,
        partial,
        len(checked) - len(cited),
        trail.gaps if trail is not None else 0,
        verification.agreement,
        len(verification.unknown_identifiers),
        verification.dropped_citations,
        whole,
    )
    return Confidence(level, score, breakdown)


def abstention(
    sure: Confidence,
    verification: Verification | None,
    trail: Trail | None,
    searched: Sequence[str],
    *,
    flow: bool,
) -> Abstention | None:
    """Why the answer is too thin to give (None: it isn't), with what was searched, where the
    trail ended, and what to ask instead."""
    claimed = sure.breakdown
    if verification is None:
        return None  # nothing was checked: confidence says so, there's no evidence to judge
    reason = None
    if claimed.claims_cited and claimed.claims_verified == 0 and claimed.claims_partial == 0:
        reason = "nothing it cites shows what it says"
    elif not claimed.claims_cited:
        reason = "it cites no code"
    elif flow and trail is not None and trail.gaps and not trail.reaches_data:
        reason = "the flow's trace stops where the graph has no link"
    elif sure.score < ABSTAIN:
        reason = "its evidence is too thin"
    if reason is None:
        return None
    return Abstention(
        tuple(searched), trail if flow else None, reason, _suggestion(trail, verification)
    )


def _suggestion(trail: Trail | None, verification: Verification) -> str:
    whole = verification.whole
    if trail is not None and trail.end and not trail.reaches_data:
        return f"Ask about {trail.end} directly, or trace from it."
    if whole is not None and whole.missing:
        return f"Ask about {', '.join(whole.missing)} on its own."
    return "Name the page, endpoint or function to start from, or a file or layer to look in."
