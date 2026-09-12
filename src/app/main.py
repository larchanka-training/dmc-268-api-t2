"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api import api_router
from app.core.config import get_settings
from app.core.logging_config import configure_logging
from app.core.middleware import RequestIdMiddleware
from app.db.session import engine

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown."""

    yield

    await engine.dispose()


def create_app() -> FastAPI:
    """Build the FastAPI application."""

    configure_logging(get_settings().logging)

    app = FastAPI(
        title="DMC-268 API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)

    @app.get("/")
    async def read_root() -> dict[str, str]:
        """Welcome payload."""
        return {"message": "Welcome to DMC-268 Team 2 API"}

    return app


app = create_app()
