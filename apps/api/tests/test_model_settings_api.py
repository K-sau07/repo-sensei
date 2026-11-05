"""An org's model and effort per step (10.7.2) over HTTP: admins read and raise them, a step
can't go below recommended, and each change is audited."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.routers.ask import _cached_as  # pyright: ignore[reportPrivateUsage]
from sensei_core.audit import AuditAction
from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import AuditEvent, Membership, Org, Role
from sensei_core.orgs import model_choices
from sensei_core.orgs.model_choices import StepChoice
from sensei_core.orgs.service import OrgService
from sensei_engine.agent.loop import Mode
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
MODELS = f"{API}/orgs/models-org/settings/models"


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def test_an_admin_raises_a_step_and_it_is_kept_and_audited(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await sign_in(api_app, client, db_session)
    made = await client.post(f"{API}/orgs", json={"name": "Models", "slug": "models-org"},
                             headers=headers)  # fmt: skip
    assert made.status_code == 201
    org = await db_session.get(Org, made.json()["id"])
    assert org is not None

    body = (await client.get(MODELS)).json()
    llm = api_app.state.settings.anthropic
    assert body["models"] == {"fast": llm.fast_model, "standard": llm.standard_model,
                              "deep": llm.deep_model}  # fmt: skip
    steps = {s["step"]: s for s in body["steps"]}
    assert list(steps) == ["answers", "diagrams", "fixes"]
    assert all((s["tier"], s["effort"]) == ("recommended", "recommended") for s in steps.values())
    assert steps["answers"]["tiers"] == ["recommended", "deep"]
    assert steps["fixes"]["efforts"] == ["recommended", "high"]

    raised = await client.patch(MODELS, json={"answers": {"tier": "deep", "effort": "high"}},
                                headers=headers)  # fmt: skip
    assert raised.status_code == 200
    answers = next(s for s in raised.json()["steps"] if s["step"] == "answers")
    assert (answers["tier"], answers["effort"]) == ("deep", "high")
    await db_session.refresh(org)
    assert org.model_choices == {"answers": {"tier": "deep", "effort": "high"}}
    assert (await model_choices.load(db_session, org.id)).answers == StepChoice(
        tier="deep", effort="high"
    )
    await set_org_context(db_session, org.id)
    (event,) = (await db_session.scalars(select(AuditEvent).where(
        AuditEvent.org_id == org.id, AuditEvent.action == AuditAction.ORG_MODELS_UPDATE.value,
    ))).all()  # fmt: skip
    assert event.meta == {"answers": {"from": {"tier": "recommended", "effort": "recommended"},
                                      "to": {"tier": "deep", "effort": "high"}}}  # fmt: skip
    assert event.actor_user_id == owner.id

    # The same again changes nothing and isn't audited again; putting it back is.
    await client.patch(MODELS, json={"answers": {"tier": "deep", "effort": "high"}},
                       headers=headers)  # fmt: skip
    back = await client.patch(MODELS, json={"answers": {}}, headers=headers)
    assert back.status_code == 200
    await db_session.refresh(org)
    assert org.model_choices == {}
    count = await db_session.scalars(select(AuditEvent.id).where(
        AuditEvent.org_id == org.id, AuditEvent.action == AuditAction.ORG_MODELS_UPDATE.value,
    ))  # fmt: skip
    assert len(count.all()) == 2

    lower = await client.patch(MODELS, json={"answers": {"tier": "standard"}}, headers=headers)
    assert lower.status_code == 422
    assert lower.json()["error"]["code"] == "invalid_model_choice"
    unknown = await client.patch(MODELS, json={"summaries": {"tier": "deep"}}, headers=headers)
    assert unknown.status_code == 422

    member = await make_user(db_session)
    db_session.add(Membership(org_id=org.id, user_id=member.id, role=Role.MEMBER))
    await db_session.flush()
    _, member_headers = await sign_in(api_app, client, db_session, member)
    assert (await client.get(MODELS)).status_code == 403
    refused = await client.patch(MODELS, json={"fixes": {"tier": "deep"}}, headers=member_headers)
    assert refused.status_code == 403


def test_answers_made_on_a_raised_floor_are_cached_apart() -> None:
    assert _cached_as(Mode.FAST, StepChoice()) == "fast"
    assert _cached_as(Mode.FAST, StepChoice(tier="deep", effort="high")) == "fast+dh"
    assert _cached_as(Mode.DEEP, StepChoice(effort="medium")) == "deep+rm"
    assert all(len(_cached_as(mode, StepChoice(tier="deep", effort="medium"))) <= 16
               for mode in Mode)  # fmt: skip
