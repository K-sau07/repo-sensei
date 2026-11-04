"""An org's usage (10.7.2) over HTTP: its spend against the caps, the last 30 days by day, model
and person, and the limits; admins only."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import Membership, Org, Role, UsageEvent, UsageKind
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    api_app.state.org_service = OrgService(MemoryMailer(), api_app.state.settings.web.origin)
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _spent(org: Org, user_id: object, cost: str, ago: timedelta, **extra: object) -> UsageEvent:
    fields: dict[str, object] = {"kind": UsageKind.LLM, "provider": "anthropic",
                                 "model": "claude-sonnet-5-5", "tokens_in": 1000,
                                 "tokens_out": 100, **extra}  # fmt: skip
    return UsageEvent(org_id=org.id, user_id=user_id, cost_usd=Decimal(cost), price_version="t",
                      created_at=datetime.now(UTC) - ago, **fields)  # fmt: skip


async def test_an_admin_reads_the_orgs_spend_against_its_caps(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await sign_in(api_app, client, db_session)
    made = await client.post(f"{API}/orgs", json={"name": "Spend", "slug": "spend-org"},
                             headers=headers)  # fmt: skip
    assert made.status_code == 201
    org = await db_session.get(Org, made.json()["id"])
    assert org is not None
    await set_org_context(db_session, org.id)
    db_session.add_all([
        _spent(org, owner.id, "1.25", timedelta(0)),
        _spent(org, owner.id, "0.75", timedelta(0), kind=UsageKind.EMBEDDING, provider="voyage",
               model="voyage-code-3"),
        _spent(org, None, "2", timedelta(days=3)),  # an index run's: no one's
        _spent(org, owner.id, "9", timedelta(days=40)),  # older than the window
    ])  # fmt: skip
    await db_session.flush()

    body = (await client.get(f"{API}/orgs/spend-org/usage")).json()
    today = body["today"]
    assert Decimal(today["spent_usd"]) == Decimal("2.00")
    assert Decimal(today["cap_usd"]) == api_app.state.settings.agent.daily_cost_cap_usd
    assert len(body["days"]) == 30
    assert Decimal(body["days"][-1]["cost_usd"]) == Decimal("2.00")
    assert body["days"][-1]["by_kind"] == {"llm": "1.250000", "embedding": "0.750000"}
    assert sum(Decimal(d["cost_usd"]) for d in body["days"]) == Decimal(4)  # not the 40-day one
    sonnet, voyage = body["models"]
    assert (sonnet["model"], sonnet["calls"], Decimal(sonnet["cost_usd"])) == (
        "claude-sonnet-5-5", 2, Decimal("3.25"),
    )  # fmt: skip
    assert voyage["kind"] == "embedding"
    people = {(p["login"], Decimal(p["cost_usd"])) for p in body["people"]}
    assert people == {(None, Decimal(2)), (owner.login, Decimal(2))}  # an index run is no one's
    assert body["limits"]["asks_per_day"] == api_app.state.settings.limits.asks_per_day

    member = await make_user(db_session)
    db_session.add(Membership(org_id=org.id, user_id=member.id, role=Role.MEMBER))
    await db_session.flush()
    await sign_in(api_app, client, db_session, member)
    refused = await client.get(f"{API}/orgs/spend-org/usage")
    assert refused.status_code == 403
