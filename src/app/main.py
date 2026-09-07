"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown."""
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(title="DMC-268 API", version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)

    @app.get("/")
    async def read_root() -> dict[str, str]:
        """Welcome payload."""
        return {"message": "Welcome to DMC-268 Team 2 API"}

    return app


app = create_app()
