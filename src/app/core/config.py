"""Application settings loaded from environment variables / .env file."""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the application."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "dmc"
    postgres_password: str = "dmc"
    postgres_db: str = "dmc"

    redis_url: str = "redis://localhost:6379/0"

    @property
    def async_db_url(self) -> str:
        """Async SQLAlchemy URL for PostgreSQL."""
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
