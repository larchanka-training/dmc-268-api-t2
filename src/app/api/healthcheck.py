"""Healthcheck endpoint (liveness): always responds 200, never touches dependencies."""

from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["healthcheck"])


@router.get("/healthcheck")
async def healthcheck() -> dict[str, Any]:
    """Liveness probe: the process is up and able to serve requests."""
    return {"status": "ok"}
