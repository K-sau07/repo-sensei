"""OAuth 2.1 (7.2): the authorization server MCP clients and the CLI sign in through.

At the origin's root (the web app forwards them): the authorization server's metadata
(``/.well-known/oauth-authorization-server``, RFC 8414) and the protected resources'
(``/.well-known/oauth-protected-resource/mcp`` and ``…/api``, RFC 9728; the root one is the MCP
server's). Under the API:

* ``GET /oauth/authorize`` checks the request and sends the browser to the consent page
  (``/oauth/consent?request=…``), or back to the client with an error (when the client and its
  redirect URI are known good), or to the consent page with the error (when they aren't);
* ``POST /oauth/token`` (form): ``authorization_code`` (with the PKCE verifier),
  ``refresh_token`` (rotating), and the device code; RFC 6749's error shape, ``no-store``;
* ``POST /oauth/register`` (RFC 7591), ``POST /oauth/device/code`` (RFC 8628) and
  ``POST /oauth/revoke`` (RFC 7009);
* for the web app's pages, signed in: the request the consent page shows and the user's answer,
  and the same for a device's code.

API keys and access tokens aren't let in here (see ``sensei_api.app``): a browser session
decides.
"""

from __future__ import annotations

import time
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from sensei_api.auth import CurrentUser, RequiredSession, get_oauth_service
from sensei_api.deps import DbSession, RedisClient
from sensei_api.errors import AppError
from sensei_api.orgs import OrgServiceDep
from sensei_api.ratelimit import TokenBucket
from sensei_core.context import current_client_ip
from sensei_core.oauth.clients import (
    CLI_CLIENT_ID,
    GRANT_CODE,
    GRANT_DEVICE,
    GRANT_REFRESH,
    ClientError,
    is_loopback,
    register,
)
from sensei_core.oauth.service import OAuthError, OAuthService, Pending, show_user_code

router = APIRouter(prefix="/oauth", tags=["oauth"])
well_known = APIRouter(tags=["oauth"], include_in_schema=False)

OAuth = Annotated[OAuthService, Depends(get_oauth_service)]
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
REGISTRATIONS_PER_MINUTE = 10


def _oauth_error(error: OAuthError) -> JSONResponse:
    """RFC 6749 §5.2's shape (not the API's envelope: clients parse it)."""
    headers = dict(NO_STORE)
    if error.status == 401:
        headers["WWW-Authenticate"] = 'Bearer error="invalid_client"'
    return JSONResponse(
        {"error": error.error, "error_description": error.description},
        status_code=error.status,
        headers=headers,
    )


# ---------------------------------------------------------------- discovery


@well_known.get("/.well-known/oauth-authorization-server")
async def server_metadata(oauth: OAuth) -> JSONResponse:
    return JSONResponse(oauth.server_metadata(), headers={"Cache-Control": "max-age=3600"})


@well_known.get("/.well-known/oauth-protected-resource")
@well_known.get("/.well-known/oauth-protected-resource/mcp")
async def mcp_resource_metadata(oauth: OAuth) -> JSONResponse:
    return JSONResponse(
        oauth.resource_metadata(oauth.resources.mcp), headers={"Cache-Control": "max-age=3600"}
    )


@well_known.get("/.well-known/oauth-protected-resource/api")
async def api_resource_metadata(oauth: OAuth) -> JSONResponse:
    return JSONResponse(
        oauth.resource_metadata(oauth.resources.api), headers={"Cache-Control": "max-age=3600"}
    )


# ---------------------------------------------------------------- the client's side


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_client(
    request: Request, db: DbSession, redis: RedisClient, oauth: OAuth
) -> JSONResponse:
    """Dynamic client registration (RFC 7591): public clients only."""
    wait = await TokenBucket(
        redis, name="oauth_register", per_minute=REGISTRATIONS_PER_MINUTE
    ).take(current_client_ip() or "unknown")
    if wait is not None:
        return JSONResponse(
            {"error": "slow_down", "error_description": "Too many registrations; try later."},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )
    try:
        body: Any = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _oauth_error(OAuthError("invalid_client_metadata", "Send the metadata as JSON."))
    try:
        client = await register(db, body)  # pyright: ignore[reportUnknownArgumentType]
    except ClientError as error:
        return _oauth_error(OAuthError(error.error, error.description))
    grants = client.meta.get("grant_types", [GRANT_CODE, GRANT_REFRESH])
    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": client.client_name,
            "client_uri": client.client_uri,
            "redirect_uris": client.redirect_uris,
            "grant_types": grants,
            "response_types": ["code"] if GRANT_CODE in grants else [],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
        headers=NO_STORE,
    )


@router.get("/authorize")
async def authorize(request: Request, db: DbSession, oauth: OAuth) -> RedirectResponse:
    """Check the client's request; on to the consent page (or back with an error)."""
    q = request.query_params
    consent = f"{oauth.issuer}/oauth/consent"
    try:
        handle, _ = await oauth.begin(
            db,
            client_id=q.get("client_id"),
            redirect_uri=q.get("redirect_uri"),
            response_type=q.get("response_type"),
            code_challenge=q.get("code_challenge"),
            code_challenge_method=q.get("code_challenge_method"),
            scope=q.get("scope"),
            state=q.get("state"),
            resource=q.get("resource"),
        )
    except OAuthError as error:
        redirect_uri = q.get("redirect_uri")
        if error.redirect and redirect_uri:
            return RedirectResponse(
                oauth.error_redirect(redirect_uri, error, q.get("state")), status_code=302
            )
        # The client or its redirect URI can't be trusted: say so here, send nobody anywhere.
        query = urlencode({"error": error.error, "error_description": error.description})
        return RedirectResponse(f"{consent}?{query}", status_code=302)
    return RedirectResponse(f"{consent}?{urlencode({'request': handle})}", status_code=302)


@router.post("/token")
async def token(
    *,
    db: DbSession,
    oauth: OAuth,
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    code: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
    device_code: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
    resource: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    try:
        if grant_type == GRANT_CODE:
            issued = await oauth.exchange_code(
                db, client_id=client_id, code=code, code_verifier=code_verifier,
                redirect_uri=redirect_uri, resource=resource,
            )  # fmt: skip
        elif grant_type == GRANT_REFRESH:
            issued = await oauth.refresh(
                db, client_id=client_id, refresh_token=refresh_token, scope=scope,
                resource=resource,
            )  # fmt: skip
        elif grant_type == GRANT_DEVICE:
            issued = await oauth.device_poll(db, client_id=client_id, device_code=device_code)
        else:
            raise OAuthError("unsupported_grant_type", f"Unsupported grant_type: {grant_type}.")
    except OAuthError as error:
        # Answered, not raised: what was done stays (a replayed code or refresh token revokes
        # its family, and that must stick though the request fails).
        return _oauth_error(error)
    return JSONResponse(issued.as_json(), headers=NO_STORE)


@router.post("/device/code")
async def device_code(
    db: DbSession,
    oauth: OAuth,
    client_id: Annotated[str, Form()] = CLI_CLIENT_ID,
    scope: Annotated[str | None, Form()] = None,
    resource: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    try:
        found = await oauth.device_authorize(
            db, client_id=client_id, scope=scope, resource=resource
        )
    except OAuthError as error:
        return _oauth_error(error)
    return JSONResponse(found, headers=NO_STORE)


@router.post("/revoke")
async def revoke(
    db: DbSession, oauth: OAuth, token: Annotated[str | None, Form()] = None
) -> Response:
    """RFC 7009: always 200 (whether there was such a token isn't said)."""
    await oauth.revoke(db, token)
    return Response(status_code=200, headers=NO_STORE)


# ---------------------------------------------------------------- the web app's side


class ConsentOut(BaseModel):
    client_name: str
    client_uri: str | None
    # Where the answer goes: its host is shown (a localhost one warned about).
    redirect_host: str
    redirect_is_local: bool
    # For a metadata document client: the host it is published on.
    client_host: str | None
    scopes: list[str]
    resource: str


class DecisionIn(BaseModel):
    org: str = Field(min_length=1, max_length=100)
    approve: bool


class DecisionOut(BaseModel):
    redirect_to: str


def _consent(found: Pending) -> ConsentOut:
    client, grant = found.client, found.grant
    published = client.client_id if client.client_id.startswith("https://") else None
    return ConsentOut(
        client_name=client.client_name,
        client_uri=client.client_uri,
        redirect_host=urlsplit(grant.redirect_uri).netloc,
        redirect_is_local=is_loopback(grant.redirect_uri),
        client_host=urlsplit(published).netloc if published else None,
        scopes=list(grant.scopes),
        resource=grant.resource,
    )


def _shown(error: OAuthError) -> AppError:
    return AppError(error.description, code=f"oauth_{error.error}", status_code=400)


@router.get("/requests/{handle}", response_model=ConsentOut)
async def consent_request(
    handle: str, _session: RequiredSession, db: DbSession, oauth: OAuth
) -> ConsentOut:
    """What a client asks, for the consent page."""
    try:
        return _consent(await oauth.pending(db, handle))
    except OAuthError as error:
        raise _shown(error) from None


@router.post("/requests/{handle}/decision", response_model=DecisionOut)
async def consent_decision(
    *,
    handle: str,
    body: DecisionIn,
    user: CurrentUser,
    _session: RequiredSession,
    db: DbSession,
    orgs: OrgServiceDep,
    oauth: OAuth,
) -> DecisionOut:
    """The user's answer, for one of their orgs: where to send the browser next."""
    access = await orgs.access(db, user, body.org)
    try:
        return DecisionOut(redirect_to=await oauth.decide(db, handle, access, approve=body.approve))
    except OAuthError as error:
        raise _shown(error) from None


class DeviceOut(BaseModel):
    client_name: str
    user_code: str
    scopes: list[str]
    resource: str


class DeviceDecisionIn(DecisionIn):
    user_code: str = Field(min_length=4, max_length=16)


@router.get("/device/{user_code}", response_model=DeviceOut)
async def device_request(
    user_code: str, _session: RequiredSession, db: DbSession, oauth: OAuth
) -> DeviceOut:
    """What a device asks, for the ``/device`` page."""
    try:
        found, client = await oauth.device_pending(db, user_code)
    except OAuthError as error:
        raise _shown(error) from None
    return DeviceOut(
        client_name=client.client_name,
        user_code=show_user_code(found.user_code),
        scopes=list(found.scopes),
        resource=found.resource,
    )


@router.post("/device/decision", status_code=status.HTTP_204_NO_CONTENT)
async def device_decision(
    *,
    body: DeviceDecisionIn,
    user: CurrentUser,
    _session: RequiredSession,
    db: DbSession,
    orgs: OrgServiceDep,
    oauth: OAuth,
) -> None:
    access = await orgs.access(db, user, body.org)
    try:
        await oauth.device_decide(db, body.user_code, access, approve=body.approve)
    except OAuthError as error:
        raise _shown(error) from None
