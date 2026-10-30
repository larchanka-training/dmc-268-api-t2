"""API routers aggregated under a single mount point."""

from fastapi import APIRouter

from app.api.github_auth import router as github_auth_router
from app.api.github_webhook import router as github_webhook_router
from app.api.healthcheck import router as healthcheck_router
from app.api.pull_requests import router as pull_requests_router
from app.api.repositories import router as repositories_router
from app.api.repository_insights import router as repository_insights_router
from app.api.review_start import router as review_start_router
from app.api.review_stream import router as review_stream_router
from app.api.reviews import router as reviews_router

api_router = APIRouter()
api_router.include_router(github_auth_router)
api_router.include_router(healthcheck_router)
api_router.include_router(github_webhook_router)
api_router.include_router(review_stream_router)
api_router.include_router(reviews_router)
api_router.include_router(review_start_router)
api_router.include_router(repositories_router)
api_router.include_router(repository_insights_router)
api_router.include_router(pull_requests_router)
