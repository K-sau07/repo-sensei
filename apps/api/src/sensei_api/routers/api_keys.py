"""API keys (7.1): ``POST/GET/DELETE /orgs/{org}/api-keys``.

A member makes, lists and revokes their own keys; an admin lists and revokes everyone's. The key
itself is in the answer to ``POST`` only, once. Managed from a browser session only: a key can't
make or revoke keys (the router refuses them, see ``sensei_api.app``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep
from sensei_core.auth.api_keys import ApiKeyService
from sensei_core.models import ApiKey, User

router = APIRouter(prefix="/orgs/{org}", tags=["api-keys"])


def get_keys(request: Request) -> ApiKeyService:
    service: ApiKeyService = request.app.state.api_key_service
    return service


Keys = Annotated[ApiKeyService, Depends(get_keys)]


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=4)
    expires_in_days: int | None = None  # 30, 90 or 365; None: never


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    owner_login: str | None
    mine: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def build(cls, key: ApiKey, owner: str | None, me: uuid.UUID) -> ApiKeyOut:
        return cls(
            id=key.id,
            name=key.name,
            prefix=key.prefix,
            scopes=list(key.scopes),
            owner_login=owner,
            mine=key.user_id == me,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
            expires_at=key.expires_at,
            revoked_at=key.revoked_at,
        )


class ApiKeyCreated(ApiKeyOut):
    key: str  # shown this once


class ApiKeysOut(BaseModel):
    keys: list[ApiKeyOut]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyIn, access: OrgAccessDep, db: DbSession, keys: Keys
) -> ApiKeyCreated:
    """A new key for you in this org. Copy it now: it isn't shown again."""
    key, token = await keys.create(
        db, access, name=body.name, scopes=body.scopes, expires_in_days=body.expires_in_days
    )
    out = ApiKeyOut.build(key, access.user.login, access.user.id)
    return ApiKeyCreated(**out.model_dump(), key=token)


@router.get("/api-keys", response_model=ApiKeysOut)
async def list_api_keys(access: OrgAccessDep, db: DbSession, keys: Keys) -> ApiKeysOut:
    """Your keys here (everyone's, for an admin), newest first."""
    found = await keys.list(db, access)
    owners = {
        row.id: row.login
        for row in await db.execute(
            select(User.id, User.login).where(User.id.in_({k.user_id for k in found}))
        )
    }
    return ApiKeysOut(
        keys=[ApiKeyOut.build(k, owners.get(k.user_id), access.user.id) for k in found]
    )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: uuid.UUID, access: OrgAccessDep, db: DbSession, keys: Keys
) -> None:
    """Revoke a key (yours, or anyone's for an admin): it stops working at once."""
    await keys.revoke(db, access, key_id)
