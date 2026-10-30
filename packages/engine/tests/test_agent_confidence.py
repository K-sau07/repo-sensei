"""Confidence and abstention (4.6): the score from what the verification found, its level,
and when an answer is too thin to give."""

from __future__ import annotations

import pytest

from sensei_engine.agent.confidence import ABSTAIN, abstention, confidence
from sensei_engine.agent.tools.base import Cited, Trail
from sensei_engine.agent.verify import Checked, Claim, Verdict, Verification, Whole

CITE = Cited("a.py", "s", 1, 4, None)


def _verified(
    verdicts: list[Verdict | None],
    *,
    uncited: int = 0,
    whole: bool | None = True,
    agreement: float | None = None,
    invented: int = 0,
    dropped: int = 0,
) -> Verification:
    claims = [Checked(Claim(n, "x", 0, 1, (CITE,)), v) for n, v in enumerate(verdicts, start=1)]
    claims += [Checked(Claim(100 + n, "y", 0, 1, ()), None) for n in range(uncited)]
    answered = None if whole is None else Whole(whole, () if whole else ("db",), "")
    names = tuple(f"made_up_{n}" for n in range(invented))
    return Verification(tuple(claims), dropped, (), names, answered, 0, agreement)


@pytest.mark.parametrize(
    ("verification", "level"),
    [
        (_verified(["supported", "supported"], agreement=1.0), "high"),
        (_verified(["supported", "partial"], uncited=1, whole=False), "medium"),
        (_verified(["partial", "unsupported"], uncited=2, whole=False, agreement=0.2), "low"),
        (_verified(["supported", "supported"], invented=3), "low"),  # invented names cost
    ],
)
def test_the_score_follows_what_was_verified(verification: Verification, level: str) -> None:
    assert confidence(verification, None).level == level


def test_the_breakdown_says_what_the_score_is_made_of() -> None:
    trail = Trail(False, 0.4, 5, 1, "sym:a.py::f", "a gap")
    sure = confidence(_verified(["supported", "partial", None], uncited=1, dropped=2), trail)
    found = sure.breakdown
    assert (found.claims_total, found.claims_cited, found.claims_verified) == (4, 3, 1)
    assert (found.claims_partial, found.inferred, found.unresolved_hops) == (1, 1, 1)
    assert (found.dropped_citations, found.whole, found.agreement) == (2, True, None)
    # support 0.75 (two judged: 1 and a half), coverage 3/4, whole, the trail's 0.4; less 2 drops
    assert sure.score == round(0.45 * 0.75 + 0.2 * 0.75 + 0.2 + 0.15 * 0.4 - 0.1, 3)
    unverified = confidence(None, None)
    assert (unverified.level, unverified.score) == ("low", 0.0)


def test_an_answer_too_thin_abstains_saying_what_to_ask() -> None:
    searched = ["round 0 'create item'", "grep 'create_item'"]
    unsupported = _verified(["unsupported", "unsupported"])
    thin = abstention(confidence(unsupported, None), unsupported, None, searched, flow=False)
    assert thin is not None
    assert (thin.reason, thin.searched) == ("nothing it cites shows what it says", tuple(searched))
    assert thin.suggestion.startswith("Name the page, endpoint or function")

    uncited = _verified([], uncited=3)
    assert abstention(confidence(uncited, None), uncited, None, [], flow=False) is not None

    gap = Trail(False, 0.9, 4, 1, "sym:web/api.ts::createItem", "client call has no endpoint")
    traced = _verified(["supported", "supported"], agreement=1.0)
    broken = abstention(confidence(traced, gap), traced, gap, [], flow=True)
    assert broken is not None
    assert broken.trail_end == gap
    assert broken.suggestion == "Ask about sym:web/api.ts::createItem directly, or trace from it."
    # The same answer to a question that isn't about a flow stands.
    assert abstention(confidence(traced, gap), traced, gap, [], flow=False) is None


def test_a_sure_answer_does_not_abstain() -> None:
    good = _verified(["supported", "supported", "partial"], agreement=0.8)
    sure = confidence(good, None)
    assert sure.score > ABSTAIN
    assert abstention(sure, good, None, [], flow=True) is None
