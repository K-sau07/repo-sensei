"""Which model and effort an org's steps run on (10.7.2).

Each step's recommended setting is what the accuracy checks run on. An admin may raise a step
(a larger model, more effort) but never lower it: a choice is a floor, so a step that already
runs higher for a hard case (an answer that crosses layers, a fix with nothing to start from)
still does. Summaries aren't a choice: they are cached and carried from commit to commit, so a
second model would mix two kinds of summary in one index.

Kept on the org as JSON; a value this version doesn't know reads as recommended, so an old or
hand-edited row never stops a run.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import Org

TierChoice = Literal["recommended", "standard", "deep"]
EffortChoice = Literal["recommended", "medium", "high"]

# From least to most: a floor only ever raises what a step was going to use.
TIER_ORDER: tuple[str, ...] = ("fast", "standard", "deep")
EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


class Step(enum.StrEnum):
    ANSWERS = "answers"  # Ask: the agent's answer turns (not its search and checks)
    DIAGRAMS = "diagrams"  # a diagram's labels
    FIXES = "fixes"  # every agent turn of a fix or a change


class StepChoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: TierChoice = "recommended"
    effort: EffortChoice = "recommended"

    @property
    def recommended(self) -> bool:
        return self.tier == "recommended" and self.effort == "recommended"


RECOMMENDED = StepChoice()

# What each step may be raised to (besides recommended).
OPTIONS: dict[Step, tuple[tuple[TierChoice, ...], tuple[EffortChoice, ...]]] = {
    Step.ANSWERS: (("deep",), ("medium", "high")),
    Step.DIAGRAMS: (("standard", "deep"), ("medium", "high")),
    Step.FIXES: (("deep",), ("high",)),
}


class InvalidChoice(ValueError):
    """A tier or effort the step can't be set to."""


class ModelChoices(BaseModel):
    model_config = ConfigDict(frozen=True)

    answers: StepChoice = RECOMMENDED
    diagrams: StepChoice = RECOMMENDED
    fixes: StepChoice = RECOMMENDED

    def of(self, step: Step) -> StepChoice:
        choice: StepChoice = getattr(self, step.value)
        return choice

    @classmethod
    def read(cls, raw: object) -> ModelChoices:
        """From the stored JSON: each step as it was saved if it is still allowed, else
        recommended."""
        found: dict[str, StepChoice] = {}
        if isinstance(raw, dict):
            stored = cast("dict[str, Any]", raw)
            for step in Step:
                value = stored.get(step.value)
                try:
                    choice = StepChoice.model_validate(value)
                    check(step, choice)
                except (ValidationError, InvalidChoice):
                    continue
                found[step.value] = choice
        return cls(**found)

    def stored(self) -> dict[str, dict[str, str]]:
        """The JSON kept: only the steps not on recommended."""
        return {
            step.value: self.of(step).model_dump() for step in Step if not self.of(step).recommended
        }


def check(step: Step, choice: StepChoice) -> None:
    tiers, efforts = OPTIONS[step]
    if choice.tier != "recommended" and choice.tier not in tiers:
        raise InvalidChoice(f"{step.value} can't run on the {choice.tier} tier")
    if choice.effort != "recommended" and choice.effort not in efforts:
        raise InvalidChoice(f"{step.value} can't run at {choice.effort} effort")


def at_least(value: str, floor: str, order: tuple[str, ...]) -> str:
    """``value``, or ``floor`` when that is higher in ``order``."""
    return floor if order.index(floor) > order.index(value) else value


async def load(db: AsyncSession, org_id: uuid.UUID, *, lock: bool = False) -> ModelChoices:
    """The org's choices; ``lock``: the row held until the transaction ends (to change them, so
    two admins' changes at once both keep)."""
    query = select(Org.model_choices).where(Org.id == org_id)
    raw = await db.scalar(query.with_for_update() if lock else query)
    return ModelChoices.read(raw)


async def save(db: AsyncSession, org_id: uuid.UUID, choices: ModelChoices) -> None:
    await db.execute(update(Org).where(Org.id == org_id).values(model_choices=choices.stored()))
