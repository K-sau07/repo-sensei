"""Indexing reference data shared with the web app (stages and what they unlock)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sensei_core.indexing.features import UNLOCKS, Feature
from sensei_core.indexing.state import STAGE_WEIGHTS, STAGES
from sensei_core.models import Stage

router = APIRouter(prefix="/indexing", tags=["indexing"])


class StageInfo(BaseModel):
    stage: Stage
    weight: int = Field(description="Typical share of an index run, out of 100.")
    unlocks: list[Feature] = Field(description="Features available once this stage is done.")


class StagesOut(BaseModel):
    stages: list[StageInfo] = Field(description="In the order they run.")


@router.get("/stages", response_model=StagesOut)
async def indexing_stages() -> StagesOut:
    """Indexing stages in order, and the features each one unlocks."""
    return StagesOut(
        stages=[
            StageInfo(
                stage=stage, weight=STAGE_WEIGHTS[stage], unlocks=list(UNLOCKS.get(stage, ()))
            )
            for stage in STAGES
        ]
    )
