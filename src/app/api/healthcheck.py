"""Healthcheck endpoint (liveness): always responds 200, never touches dependencies."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings

router = APIRouter(tags=["healthcheck"])


@router.get("/healthcheck")
async def healthcheck(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Liveness probe: the process is up and able to serve requests."""
    return {"status": "ok", "commit": settings.git_sha}
