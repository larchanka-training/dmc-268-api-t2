"""Healthcheck endpoint (liveness): always responds 200, never touches dependencies."""

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["healthcheck"])


@router.get("/healthcheck")
async def healthcheck() -> dict[str, str]:
    """Liveness probe: the process is up and able to serve requests."""
    settings = get_settings()
    return {"status": "ok", "commit": settings.git_sha}
