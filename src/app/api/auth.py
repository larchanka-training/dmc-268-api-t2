"""Authenticated principal seam for the future accepted session integration."""

from dataclasses import dataclass
from uuid import UUID

from app.api.errors import ApiError


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    user_id: UUID


async def get_authenticated_principal() -> AuthenticatedPrincipal:
    """Fail closed until cookie and PostgreSQL session verification is connected."""
    raise ApiError(401, "AUTHENTICATION_REQUIRED", "Authentication is required.")
