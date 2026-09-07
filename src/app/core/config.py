"""Application settings loaded from environment variables / .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Postgres(BaseModel):
    """PostgreSQL connection settings (POSTGRES__* env variables)."""

    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str = "postgres"
    db: str = "postgres"

    @property
    def url(self) -> str:
        """Async SQLAlchemy URL for PostgreSQL."""
        user = quote_plus(self.user)
        password = quote_plus(self.password)
        return f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{self.db}"


class Redis(BaseModel):
    """Redis connection settings (REDIS__* env variables)."""

    url: str = "redis://localhost:6379/0"


class Logging(BaseModel):
    """Logging settings (LOGGING__* env variables)."""

    level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"] = "INFO"
    format: Literal["console", "json"] = "console"
    file_enabled: bool = True
    file_path: Path = Path("logs") / "app.log"

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: object) -> object:
        """Accept level names case-insensitively ("info" == "INFO")."""
        if isinstance(value, str):
            return value.upper()
        return value


class Settings(BaseSettings):
    """Runtime configuration for the application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
    )

    postgres: Postgres = Field(default_factory=Postgres)
    redis: Redis = Field(default_factory=Redis)
    logging: Logging = Field(default_factory=Logging)


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
