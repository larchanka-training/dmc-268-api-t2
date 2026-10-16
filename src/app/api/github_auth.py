"""GitHub browser OAuth and current session HTTP endpoints."""

import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated
from urllib.parse import unquote, urlencode

import httpx
import jwt
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from app.api.auth import VerifiedSession, get_verified_session
from app.api.errors import ApiError
from app.application.github_ports import GithubFailure
from app.core.config import Settings, get_settings
from app.db.models import AuthSession, GithubIdentity, GithubOAuthFlow, User
from app.db.session import get_session
from app.vcs.github import GithubClient
from app.vcs.github_oauth import exchange_code

router = APIRouter(prefix="/api/v1", tags=["auth"])
_FLOW_COOKIE = "__Host-dmc_oauth"
_FLOW_SECONDS = 600


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_return_to(value: str | None) -> str:
    if not value or len(value) > 2048:  # noqa: PLR2004 -- bound redirect size
        return "/"
    decoded = value
    for _ in range(5):
        if (
            not decoded.startswith("/")
            or decoded.startswith("//")
            or "\\" in decoded
            or any(ord(char) <= 32 or ord(char) == 127 for char in decoded)  # noqa: PLR2004
        ):
            return "/"
        new = unquote(decoded)
        if new == decoded:
            return value
        decoded = new
    return "/"


async def get_oauth_http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=False,
        headers={"User-Agent": "dmc-268-api-t2", "X-GitHub-Api-Version": "2022-11-28"},
    ) as client:
        yield client


@router.get("/auth/github/start", status_code=302)
async def start_github_oauth(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    return_to: str | None = None,
) -> RedirectResponse:
    try:
        settings.auth.check_ready()
    except ValueError, UnicodeError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = await session.scalar(func.clock_timestamp())
    session.add(
        GithubOAuthFlow(
            state_hash=_digest(state),
            browser_hash=_digest(browser),
            return_to=_safe_return_to(return_to),
            expires_at=now + timedelta(seconds=_FLOW_SECONDS),
        )
    )
    await session.commit()
    query = urlencode(
        {
            "client_id": settings.auth.github_client_id,
            "redirect_uri": settings.auth.github_redirect_uri,
            "state": state,
        }
    )
    response = RedirectResponse(
        f"https://github.com/login/oauth/authorize?{query}", status_code=302
    )
    response.set_cookie(
        _FLOW_COOKIE,
        browser,
        max_age=_FLOW_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _auth_error(code: str) -> RedirectResponse:
    response = RedirectResponse(f"/login?error={code}", status_code=303)
    response.delete_cookie(_FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/auth/github/callback", status_code=303)
# Keep OAuth query inputs and trusted dependencies explicit, with early denial gates.
async def finish_github_oauth(  # noqa: PLR0911, PLR0913, PLR0917
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    http: Annotated[httpx.AsyncClient, Depends(get_oauth_http)],
    session: Annotated[AsyncSession, Depends(get_session)],
    state: str = "",
    code: str = "",
    error: str | None = None,
) -> RedirectResponse:
    browser = request.cookies.get(_FLOW_COOKIE, "")
    if not state or not browser:
        return _auth_error("invalid_state")
    flow = (
        await session.execute(
            delete(GithubOAuthFlow)
            .where(
                GithubOAuthFlow.state_hash == _digest(state),
                GithubOAuthFlow.browser_hash == _digest(browser),
                GithubOAuthFlow.expires_at > func.clock_timestamp(),
            )
            .returning(GithubOAuthFlow.return_to)
        )
    ).scalar_one_or_none()
    # Commit consumption before any provider request; failure cannot resurrect it.
    await session.commit()
    if flow is None:
        return _auth_error("invalid_state")
    try:
        settings.auth.check_ready()
    except ValueError, UnicodeError:
        return _auth_error("auth_unavailable")
    if error is not None:
        return _auth_error("github_denied")
    if not code:
        return _auth_error("invalid_code")
    try:
        token = await exchange_code(http, settings.auth, code)
        external_id, login = await GithubClient(http).get_identity(token)
    except GithubFailure, httpx.RequestError, ValueError, UnicodeError:
        return _auth_error("github_unavailable")
    encrypted = (
        Fernet(settings.auth.credential_key.get_secret_value())
        .encrypt(token.encode())
        .decode()
    )
    # Serialize first-login creation by stable GitHub ID (including concurrent tabs).
    await session.execute(
        select(func.pg_advisory_xact_lock(int(external_id) % (2**63)))
    )
    identity = await session.scalar(
        select(GithubIdentity).where(GithubIdentity.external_id == external_id)
    )
    if identity is None:
        user = User()
        session.add(user)
        await session.flush()
        identity = GithubIdentity(user_id=user.id, external_id=external_id)
        session.add(identity)
    identity.login, identity.encrypted_token = login, encrypted
    now = (await session.scalar(func.clock_timestamp())).replace(microsecond=0)
    expires = now + timedelta(minutes=30)
    sid = uuid.uuid4()
    session.add(
        AuthSession(
            id=sid,
            user_id=identity.user_id,
            expires_at=expires,
            csrf_token=secrets.token_urlsafe(32),
        )
    )
    encoded = jwt.encode(
        {
            "sub": str(identity.user_id),
            "sid": str(sid),
            "iat": now,
            "exp": expires,
            "iss": "dmc268",
            "aud": "dmc268-browser",
        },
        settings.auth.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    await session.commit()
    response = RedirectResponse(flow, status_code=303)
    response.set_cookie(
        "dmc_session",
        encoded,
        max_age=1800,
        expires=expires,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(_FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/me", operation_id="getCurrentUser")
async def get_current_user(
    verified: Annotated[VerifiedSession, Depends(get_verified_session)],
    response: Response,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "user": {
            "id": str(verified.user_id),
            "provider": "github",
            "external_id": verified.external_id,
            "login": verified.login,
        },
        "session": {"expires_at": verified.expires_at.isoformat()},
        "csrf_token": verified.csrf_token,
    }
