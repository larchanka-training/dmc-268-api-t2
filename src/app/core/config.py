"""Application settings loaded from environment variables / .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ProviderName = Literal["eurorouter", "ollama"]


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
        return (
            f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{self.db}"
        )


class Redis(BaseModel):
    """Redis connection settings (REDIS__* env variables)."""

    url: str = "redis://localhost:6379/0"


class Webhook(BaseModel):
    secret: SecretStr = SecretStr("")


class Github(BaseModel):
    token: SecretStr = SecretStr("")


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


class LLMConfigError(ValueError):
    """LLM provider chain is not configured; raised when the gateway is built."""


class Eurorouter(BaseModel):
    """OpenAI-compatible primary provider (LLM__EUROROUTER__* env variables)."""

    base_url: str = ""
    model: str = ""
    # Secret: delivered from CI/CD only, comma-separated in the env variable.
    api_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    timeout_s: float = Field(default=120, gt=0)

    @field_validator("base_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if value and not value.startswith("https://"):
            msg = "LLM__EUROROUTER__BASE_URL must use https://"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return [key.strip() for key in value.split(",") if key.strip()]
        return value


class Ollama(BaseModel):
    """Ollama fallback provider (LLM__OLLAMA__* env variables)."""

    base_url: str = "http://ollama:11434"
    model: str = ""
    timeout_s: float = Field(default=120, gt=0)


class LLM(BaseModel):
    """LLM gateway settings (LLM__* env variables)."""

    primary_provider: ProviderName = "eurorouter"
    fallback_enabled: bool = True
    eurorouter: Eurorouter = Field(default_factory=Eurorouter)
    ollama: Ollama = Field(default_factory=Ollama)

    prompt_version: str = "v2"
    # Per-call limits (SD §14), shared by both providers (spec §5.4).
    context_window: int = Field(default=16384, ge=16384)
    max_input_tokens: int = Field(default=10000, gt=0)
    max_output_tokens: int = Field(default=2000, gt=0)
    request_timeout_s: float = Field(default=120, gt=0)
    # Per-run budget (SD §14, spec §4.5).
    run_max_requests: int = Field(default=8, gt=0)
    run_max_input_tokens: int = Field(default=80000, gt=0)
    run_max_output_tokens: int = Field(default=16000, gt=0)
    run_deadline_s: float = Field(default=600, gt=0)
    # Reflection Pass verifications per run; 0 keeps the pass off (issue #25).
    run_max_reflections: int = Field(default=0, ge=0)
    max_chunks: int = Field(default=5, gt=0)

    @property
    def provider_chain(self) -> list[ProviderName]:
        """Providers in call order: primary, then the other one if enabled."""
        secondary: ProviderName = (
            "ollama" if self.primary_provider == "eurorouter" else "eurorouter"
        )
        if self.fallback_enabled:
            return [self.primary_provider, secondary]
        return [self.primary_provider]

    @model_validator(mode="after")
    def _check_token_limits(self) -> Self:
        if self.max_input_tokens + self.max_output_tokens > self.context_window:
            msg = "LLM__MAX_INPUT_TOKENS + LLM__MAX_OUTPUT_TOKENS exceed context window"
            raise ValueError(msg)
        return self

    def check_provider_chain(self) -> None:
        """Fail if a provider in the chain lacks its settings.

        Called when the LLM gateway is built, not at Settings() load: the API
        and workers without LLM calls must start without any LLM__* variables.
        """
        required: dict[ProviderName, dict[str, object]] = {
            "eurorouter": {
                "API_KEYS": self.eurorouter.api_keys,
                "BASE_URL": self.eurorouter.base_url,
                "MODEL": self.eurorouter.model,
            },
            "ollama": {
                "BASE_URL": self.ollama.base_url,
                "MODEL": self.ollama.model,
            },
        }
        for provider in self.provider_chain:
            missing = [name for name, value in required[provider].items() if not value]
            if missing:
                prefix = f"LLM__{provider.upper()}__"
                names = ", ".join(prefix + name for name in missing)
                msg = f"{provider} is in the provider chain but {names} is empty"
                raise LLMConfigError(msg)


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
    webhook: Webhook = Field(default_factory=Webhook)
    github: Github = Field(default_factory=Github)
    logging: Logging = Field(default_factory=Logging)
    llm: LLM = Field(default_factory=LLM)


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
