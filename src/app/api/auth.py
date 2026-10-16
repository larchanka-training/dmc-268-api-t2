"""JWT and authoritative PostgreSQL session verification."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.core.config import Settings, get_settings
from app.db.models import AuthSession, GithubIdentity
from app.db.session import get_session


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    user_id: UUID


@dataclass(frozen=True)
class VerifiedSession:
    user_id: UUID
    session_id: UUID
    external_id: str
    login: str
    expires_at: datetime
    csrf_token: str


async def get_verified_session(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VerifiedSession:
    token = request.cookies.get("dmc_session", "")
    secret = settings.auth.jwt_secret.get_secret_value()
    unauthorized = ApiError(
        401, "AUTHENTICATION_REQUIRED", "Authentication is required."
    )
    if not token or len(token) > 4096 or len(secret.encode()) < 32:  # noqa: PLR2004
        raise unauthorized
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer="dmc268",
            audience="dmc268-browser",
            options={"require": ["sub", "sid", "iat", "exp"]},
        )
        user_id, sid = UUID(claims["sub"]), UUID(claims["sid"])
    except jwt.InvalidTokenError, ValueError, TypeError, AttributeError:
        raise unauthorized from None
    try:
        row = (
            await session.execute(
                select(
                    AuthSession.user_id,
                    AuthSession.id,
                    GithubIdentity.external_id,
                    GithubIdentity.login,
                    AuthSession.expires_at,
                    AuthSession.csrf_token,
                )
                .join(GithubIdentity, GithubIdentity.user_id == AuthSession.user_id)
                .where(
                    AuthSession.id == sid,
                    AuthSession.user_id == user_id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > func.clock_timestamp(),
                )
            )
        ).one_or_none()
    except SQLAlchemyError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    if row is None:
        raise unauthorized
    return VerifiedSession(*row)


async def get_authenticated_principal(
    verified: Annotated[VerifiedSession, Depends(get_verified_session)],
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(verified.user_id)


@dataclass(frozen=True)
class VerifiedMutationIdentity:
    """Evidence supplied only by the trusted session/Origin/CSRF verifier."""

    user_id: UUID
    session_verified: bool
    origin_verified: bool
    csrf_verified: bool


async def get_verified_mutation_identity(
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
) -> VerifiedMutationIdentity:
    """Headers alone cannot prove session-bound mutation authorization."""
    raise ApiError(403, "ACCESS_DENIED", "Access is denied.")
