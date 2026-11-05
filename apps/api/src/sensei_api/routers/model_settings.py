"""An org's model and effort per step (10.7.2, admins).

Each step runs as recommended (what the accuracy checks run on) unless an admin raised it to a
larger model or more effort; a choice is a floor, never lower than recommended (see
``sensei_core.orgs.model_choices``). Which model a tier is comes from the deployment's settings.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep
from sensei_core.audit import AuditAction, audit
from sensei_core.config import Settings
from sensei_core.errors import InvalidError
from sensei_core.models import Role
from sensei_core.orgs import model_choices
from sensei_core.orgs.model_choices import (
    OPTIONS,
    EffortChoice,
    InvalidChoice,
    ModelChoices,
    Step,
    StepChoice,
    TierChoice,
)
from sensei_core.orgs.service import require_role

router = APIRouter(prefix="/orgs/{org}/settings", tags=["models"])


class StepOut(BaseModel):
    step: Step
    tier: TierChoice
    effort: EffortChoice
    tiers: list[TierChoice] = Field(description="What it may be set to, recommended first.")
    efforts: list[EffortChoice] = Field(description="What it may be set to, recommended first.")


class ModelsOut(BaseModel):
    steps: list[StepOut]
    models: dict[str, str] = Field(description="The model each tier runs on: fast, standard, deep.")


class ModelsUpdate(BaseModel):
    """The steps to change; a step left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    answers: StepChoice | None = None
    diagrams: StepChoice | None = None
    fixes: StepChoice | None = None


def _out(request: Request, choices: ModelChoices) -> ModelsOut:
    settings: Settings = request.app.state.settings
    llm = settings.anthropic
    steps: list[StepOut] = []
    for step in Step:
        tiers, efforts = OPTIONS[step]
        choice = choices.of(step)
        steps.append(StepOut(
            step=step, tier=choice.tier, effort=choice.effort,
            tiers=["recommended", *tiers], efforts=["recommended", *efforts],
        ))  # fmt: skip
    return ModelsOut(
        steps=steps,
        models={"fast": llm.fast_model, "standard": llm.standard_model, "deep": llm.deep_model},
    )


@router.get("/models", response_model=ModelsOut)
async def get_models(request: Request, access: OrgAccessDep, db: DbSession) -> ModelsOut:
    """Each step's model and effort, and what it may be raised to."""
    require_role(access, Role.ADMIN)
    return _out(request, await model_choices.load(db, access.org.id))


@router.patch("/models", response_model=ModelsOut)
async def update_models(
    body: ModelsUpdate, request: Request, access: OrgAccessDep, db: DbSession
) -> ModelsOut:
    """Raise (or put back) the model and effort of the steps given."""
    require_role(access, Role.ADMIN)
    before = await model_choices.load(db, access.org.id)
    changed: dict[str, StepChoice] = {}
    for step in Step:
        wanted: StepChoice | None = getattr(body, step.value)
        if wanted is None or wanted == before.of(step):
            continue
        try:
            model_choices.check(step, wanted)
        except InvalidChoice as exc:
            raise InvalidError(str(exc), code="invalid_model_choice") from None
        changed[step.value] = wanted
    if not changed:
        return _out(request, before)
    after = before.model_copy(update=changed)
    await model_choices.save(db, access.org.id, after)
    await audit(
        db, AuditAction.ORG_MODELS_UPDATE, actor_user_id=access.user.id, org_id=access.org.id,
        meta={step: {"from": before.of(Step(step)).model_dump(), "to": choice.model_dump()}
              for step, choice in changed.items()},
    )  # fmt: skip
    return _out(request, after)
