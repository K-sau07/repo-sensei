"""A stand-in for github.com's OAuth endpoints, for browser tests against the real API.

It behaves like GitHub's web flow closely enough to exercise everything we own: the authorize
redirect, PKCE (S256, verified exactly), the state round trip, token exchange, refresh, and the
profile/email lookups. Never used outside tests.

Who signs in: the `stub_login` cookie on this origin picks the GitHub user (tests set it per
browser context); without it, every sign-in is a brand-new user.

    uv run python scripts/e2e/github_oauth_stub.py --port 8099
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import zlib
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route


@dataclass
class Grant:
    login: str
    challenge: str
    redirect_uri: str


GRANTS: dict[str, Grant] = {}
TOKENS: dict[str, str] = {}  # access or refresh token -> login


def _user_id(login: str) -> int:
    # Stable per login, and clear of the ids used by unit tests.
    return 800_000_000 + zlib.crc32(login.encode())


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def _issue(login: str) -> dict[str, object]:
    access, refresh = f"ghu_stub{secrets.token_hex(16)}", f"ghr_stub{secrets.token_hex(16)}"
    TOKENS[access] = TOKENS[refresh] = login
    return {
        "access_token": access,
        "token_type": "bearer",
        "scope": "",
        "expires_in": 28800,
        "refresh_token": refresh,
        "refresh_token_expires_in": 15897600,
    }


async def authorize(request: Request) -> Response:
    query = request.query_params
    if query.get("code_challenge_method") != "S256" or not query.get("code_challenge"):
        return JSONResponse({"error": "pkce_required"}, status_code=400)
    login = request.cookies.get("stub_login") or f"stub-{secrets.token_hex(4)}"
    code = secrets.token_urlsafe(16)
    GRANTS[code] = Grant(login, query["code_challenge"], query["redirect_uri"])
    target = f"{query['redirect_uri']}?{urlencode({'code': code, 'state': query['state']})}"
    return RedirectResponse(target, status_code=302)


async def access_token(request: Request) -> Response:
    # GitHub takes an urlencoded body; parsed by hand to avoid a multipart dependency.
    form = {key: values[0] for key, values in parse_qs((await request.body()).decode()).items()}
    if form.get("grant_type") == "refresh_token":
        login = TOKENS.pop(str(form.get("refresh_token")), None)
        return JSONResponse(_issue(login) if login else {"error": "bad_refresh_token"})
    grant = GRANTS.pop(str(form.get("code")), None)
    if grant is None:
        return JSONResponse({"error": "bad_verification_code"})
    if _challenge(str(form.get("code_verifier"))) != grant.challenge:
        return JSONResponse({"error": "invalid_grant"})  # what GitHub says for a PKCE mismatch
    if form.get("redirect_uri") != grant.redirect_uri:
        return JSONResponse({"error": "redirect_uri_mismatch"})
    return JSONResponse(_issue(grant.login))


def _login_for(request: Request) -> str | None:
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    return TOKENS.get(token)


async def user(request: Request) -> Response:
    login = _login_for(request)
    if login is None:
        return JSONResponse({"message": "Bad credentials"}, status_code=401)
    return JSONResponse(
        {
            "id": _user_id(login),
            "login": login,
            "name": login.title(),
            "email": None,
            "avatar_url": None,
        }
    )


async def emails(request: Request) -> Response:
    login = _login_for(request)
    if login is None:
        return JSONResponse({"message": "Bad credentials"}, status_code=401)
    return JSONResponse([{"email": f"{login}@example.com", "primary": True, "verified": True}])


app = Starlette(
    routes=[
        Route("/login/oauth/authorize", authorize),
        Route("/login/oauth/access_token", access_token, methods=["POST"]),
        Route("/user", user),
        Route("/user/emails", emails),
    ]
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
