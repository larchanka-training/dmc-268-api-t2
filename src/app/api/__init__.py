"""API routers aggregated under a single mount point."""

from fastapi import APIRouter

from app.api.github_webhook import router as github_webhook_router
from app.api.healthcheck import router as healthcheck_router
from app.api.review_start import router as review_start_router
from app.api.reviews import router as reviews_router

api_router = APIRouter()
api_router.include_router(healthcheck_router)
api_router.include_router(github_webhook_router)
api_router.include_router(reviews_router)
api_router.include_router(review_start_router)
