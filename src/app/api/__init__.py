"""API routers aggregated under a single mount point."""

from fastapi import APIRouter

from app.api.healthcheck import router as healthcheck_router

api_router = APIRouter()
api_router.include_router(healthcheck_router)
