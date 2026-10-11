"""Authenticated principal seam for the future accepted session integration."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends

from app.api.errors import ApiError


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    user_id: UUID


async def get_authenticated_principal() -> AuthenticatedPrincipal:
    """Fail closed until cookie and PostgreSQL session verification is connected."""
    raise ApiError(401, "AUTHENTICATION_REQUIRED", "Authentication is required.")


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
