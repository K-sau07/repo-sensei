"""Org dependencies: resolve ``/orgs/{org}`` to the caller's membership and scope the request."""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import Depends, Path, Request

from sensei_api.auth import CurrentUser, OptionalBearer
from sensei_api.deps import DbSession
from sensei_core.errors import NotFoundError
from sensei_core.orgs.service import OrgAccess, OrgService


def get_org_service(request: Request) -> OrgService:
    service: OrgService = request.app.state.org_service
    return service


OrgServiceDep = Annotated[OrgService, Depends(get_org_service)]


async def get_org_access(
    org: Annotated[str, Path(description="Organization slug")],
    user: CurrentUser,
    key: OptionalBearer,
    db: DbSession,
    orgs: OrgServiceDep,
) -> OrgAccess:
    access = await orgs.access(db, user, org)
    if key is not None and key.org_id != access.org.id:
        # A key is for one org: another looks like no org at all (as one the user isn't in).
        raise NotFoundError("Organization not found.", code="org_not_found")
    structlog.contextvars.bind_contextvars(org_id=str(access.org.id))
    return access


OrgAccessDep = Annotated[OrgAccess, Depends(get_org_access)]
