"""/me, sessions and logout over HTTP, including the CSRF rules."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.auth import cookie_names
from sensei_core.auth.sessions import IssuedSession, SessionService
from sensei_core.models import User
from sensei_testing import make_user

pytestmark = pytest.mark.integration

API = "/api/v1"


async def _sign_in(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, user: User | None = None
) -> tuple[User, IssuedSession]:
    user = user or await make_user(db)
    service: SessionService = app.state.session_service
    issued = await service.create(db, user, ip="127.0.0.1", user_agent="pytest")
    session_cookie, csrf_cookie = cookie_names(app.state.settings)
    client.cookies.set(session_cookie, issued.token)
    client.cookies.set(csrf_cookie, issued.csrf_token)
    return user, issued


def _unsafe_headers(app: FastAPI, csrf: str | None) -> dict[str, str]:
    headers = {"Origin": app.state.settings.web.origin}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers


async def test_me_requires_a_session(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get(f"{API}/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_me_with_unknown_session_is_expired(
    api_app: FastAPI, api_client: httpx.AsyncClient
) -> None:
    api_client.cookies.set(cookie_names(api_app.state.settings)[0], "forged")
    response = await api_client.get(f"{API}/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


async def test_me_returns_the_user(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, _ = await _sign_in(api_app, api_client, db_session)
    response = await api_client.get(f"{API}/me")
    assert response.status_code == 200
    body = response.json()
    assert body["login"] == user.login
    assert body["is_staff"] is False


@pytest.mark.parametrize(
    "case",
    ["no_origin", "wrong_origin", "no_header", "header_not_cookie", "other_sessions_token"],
)
async def test_csrf_rejections(
    case: str, api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, issued = await _sign_in(api_app, api_client, db_session)
    other = await api_app.state.session_service.create(db_session, user)
    headers: dict[str, Any] = _unsafe_headers(api_app, issued.csrf_token)
    if case == "no_origin":
        headers.pop("Origin")
    elif case == "wrong_origin":
        headers["Origin"] = "https://evil.example"
    elif case == "no_header":
        headers.pop("X-CSRF-Token")
    elif case == "header_not_cookie":
        headers["X-CSRF-Token"] = "something-else"
    elif case == "other_sessions_token":
        # Header and cookie agree, but belong to a different session than the session cookie.
        api_client.cookies.set(cookie_names(api_app.state.settings)[1], other.csrf_token)
        headers["X-CSRF-Token"] = other.csrf_token

    response = await api_client.post(f"{API}/auth/logout", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert (await api_client.get(f"{API}/me")).status_code == 200  # still signed in


async def test_referer_is_accepted_when_origin_is_absent(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, issued = await _sign_in(api_app, api_client, db_session)
    origin = api_app.state.settings.web.origin
    response = await api_client.post(
        f"{API}/auth/logout",
        headers={"Referer": f"{origin}/settings/sessions", "X-CSRF-Token": issued.csrf_token},
    )
    assert response.status_code == 204


async def test_logout_revokes_and_clears_cookies(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, issued = await _sign_in(api_app, api_client, db_session)
    response = await api_client.post(
        f"{API}/auth/logout", headers=_unsafe_headers(api_app, issued.csrf_token)
    )
    assert response.status_code == 204
    set_cookie = response.headers.get_list("set-cookie")
    session_cookie, csrf_cookie = cookie_names(api_app.state.settings)
    assert any(c.startswith(f"{session_cookie}=") and "Max-Age=0" in c for c in set_cookie)
    assert any(c.startswith(f"{csrf_cookie}=") and "Max-Age=0" in c for c in set_cookie)
    api_client.cookies.set(session_cookie, issued.token)
    assert (await api_client.get(f"{API}/me")).status_code == 401


async def test_logout_all_revokes_every_session(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, issued = await _sign_in(api_app, api_client, db_session)
    service: SessionService = api_app.state.session_service
    elsewhere = await service.create(db_session, user)
    response = await api_client.post(
        f"{API}/auth/logout-all", headers=_unsafe_headers(api_app, issued.csrf_token)
    )
    assert response.status_code == 204
    assert await service.authenticate(db_session, elsewhere.token) is None


async def test_list_and_revoke_sessions(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, issued = await _sign_in(api_app, api_client, db_session)
    other = await api_app.state.session_service.create(db_session, user)

    listed = (await api_client.get(f"{API}/me/sessions")).json()
    assert {s["id"] for s in listed} == {str(issued.session.id), str(other.session.id)}
    assert [s["current"] for s in listed if s["id"] == str(issued.session.id)] == [True]

    headers = _unsafe_headers(api_app, issued.csrf_token)
    response = await api_client.delete(f"{API}/me/sessions/{other.session.id}", headers=headers)
    assert response.status_code == 204
    again = await api_client.delete(f"{API}/me/sessions/{other.session.id}", headers=headers)
    assert again.status_code == 404


async def test_cannot_revoke_someone_elses_session(
    api_app: FastAPI, api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, issued = await _sign_in(api_app, api_client, db_session)
    stranger = await make_user(db_session)
    theirs = await api_app.state.session_service.create(db_session, stranger)
    response = await api_client.delete(
        f"{API}/me/sessions/{theirs.session.id}",
        headers=_unsafe_headers(api_app, issued.csrf_token),
    )
    assert response.status_code == 404
    assert await api_app.state.session_service.authenticate(db_session, theirs.token) is not None


def test_cookie_names_follow_secure_setting() -> None:
    from sensei_core.config import Settings  # noqa: PLC0415
    from sensei_testing import prod_settings  # noqa: PLC0415

    assert cookie_names(prod_settings()) == ("__Host-session", "__Host-csrf")
    assert cookie_names(Settings(env="dev")) == ("session", "csrf")
