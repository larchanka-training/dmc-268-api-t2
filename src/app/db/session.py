"""Async database engine and session management."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import Base

__all__ = ["Base", "async_session_factory", "engine", "get_session"]

settings = get_settings()

engine = create_async_engine(settings.postgres.url, echo=False)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a database session."""
    async with async_session_factory() as session:
        yield session
