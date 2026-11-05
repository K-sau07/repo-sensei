"""An org's model and effort per step (10.7.2): floors only, read tolerantly, kept small."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from sensei_core.config import Settings
from sensei_core.models import Org
from sensei_core.orgs.model_choices import (
    EFFORT_ORDER,
    TIER_ORDER,
    InvalidChoice,
    ModelChoices,
    Step,
    StepChoice,
    at_least,
    check,
    load,
    save,
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


@pytest.fixture
async def app_engine(migrated_test_db: Settings) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(migrated_test_db.database.url.get_secret_value())
    yield engine
    await engine.dispose()


@pytest.fixture
async def org_id(migrated_test_db: Settings) -> AsyncGenerator[uuid.UUID]:
    """An org committed by the system role, removed afterwards."""
    system = create_async_engine(migrated_test_db.database.system_url.get_secret_value())
    found = uuid.uuid4()
    async with system.begin() as conn:
        await conn.execute(insert(Org).values(id=found, slug=f"floors-{found.hex[:8]}", name="F"))
    try:
        yield found
    finally:
        async with system.begin() as conn:
            await conn.execute(delete(Org).where(Org.id == found))
        await system.dispose()


@pytest.mark.integration
async def test_two_admins_changing_steps_at_once_both_keep(
    app_engine: AsyncEngine, org_id: uuid.UUID
) -> None:
    first_read, first_may_commit = asyncio.Event(), asyncio.Event()

    async def change(step: Step, choice: StepChoice, *, first: bool) -> None:
        async with AsyncSession(app_engine) as db, db.begin():
            before = await load(db, org_id, lock=True)
            if first:
                first_read.set()
                await first_may_commit.wait()  # the row stays locked until this commits
            await save(db, org_id, before.model_copy(update={step.value: choice}))

    answers = asyncio.create_task(change(Step.ANSWERS, StepChoice(tier="deep"), first=True))
    await first_read.wait()
    fixes = asyncio.create_task(change(Step.FIXES, StepChoice(effort="high"), first=False))
    await asyncio.sleep(0.2)
    assert not fixes.done()  # waiting for the first change's lock
    first_may_commit.set()
    await asyncio.gather(answers, fixes)
    async with AsyncSession(app_engine) as db:
        kept = await load(db, org_id)
    assert (kept.answers, kept.fixes) == (StepChoice(tier="deep"), StepChoice(effort="high"))
