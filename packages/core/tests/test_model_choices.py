"""An org's model and effort per step (10.7.2): floors only, read tolerantly, kept small."""

from __future__ import annotations

import pytest

from sensei_core.orgs.model_choices import (
    EFFORT_ORDER,
    TIER_ORDER,
    InvalidChoice,
    ModelChoices,
    Step,
    StepChoice,
    at_least,
    check,
)


def test_a_floor_only_raises() -> None:
    assert at_least("standard", "deep", TIER_ORDER) == "deep"
    assert at_least("deep", "standard", TIER_ORDER) == "deep"
    assert at_least("low", "medium", EFFORT_ORDER) == "medium"
    assert at_least("max", "high", EFFORT_ORDER) == "max"


@pytest.mark.parametrize(
    ("step", "choice"),
    [
        (Step.ANSWERS, StepChoice(tier="standard")),  # Sonnet only: no Opus for hard questions
        (Step.FIXES, StepChoice(effort="medium")),
    ],
)
def test_a_step_cant_be_set_below_what_it_is_checked_on(step: Step, choice: StepChoice) -> None:
    with pytest.raises(InvalidChoice):
        check(step, choice)


def test_what_is_stored_reads_back_and_anything_else_reads_as_recommended() -> None:
    choices = ModelChoices(answers=StepChoice(tier="deep"), fixes=StepChoice(effort="high"))
    stored = choices.stored()
    assert stored == {
        "answers": {"tier": "deep", "effort": "recommended"},
        "fixes": {"tier": "recommended", "effort": "high"},
    }
    assert ModelChoices.read(stored) == choices
    odd = {
        "answers": {"tier": "fast"},  # not allowed for answers
        "diagrams": {"tier": "deep", "colour": "red"},  # a key it doesn't know
        "fixes": "deep",  # not an object
        "summaries": {"tier": "deep"},  # not a step
    }
    assert ModelChoices.read(odd) == ModelChoices()
    assert ModelChoices.read(None) == ModelChoices()
    assert ModelChoices().stored() == {}
