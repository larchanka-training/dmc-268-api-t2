"""Tests for application settings loading."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Postgres, Settings

_TEST_PORT = 5433


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Settings() from reading the developer's local .env."""
    monkeypatch.chdir(tmp_path)
    # Local mode (spec §6.1): Ollama only, no Eurorouter key needed.
    monkeypatch.setenv("LLM__PRIMARY_PROVIDER", "ollama")
    monkeypatch.setenv("LLM__FALLBACK_ENABLED", "false")
    monkeypatch.setenv("LLM__OLLAMA__MODEL", "qwen:14b")


def test_settings_parse_nested_env_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings read nested POSTGRES__* / REDIS__* / LOGGING__* env variables."""
    monkeypatch.setenv("POSTGRES__HOST", "db")
    monkeypatch.setenv("POSTGRES__PORT", str(_TEST_PORT))
    monkeypatch.setenv("POSTGRES__USER", "svc")
    monkeypatch.setenv("POSTGRES__PASSWORD", "secret")
    monkeypatch.setenv("POSTGRES__DB", "dmc")
    monkeypatch.setenv("REDIS__URL", "redis://cache:6380/1")
    monkeypatch.setenv("LOGGING__LEVEL", "DEBUG")
    monkeypatch.setenv("LOGGING__FORMAT", "json")
    monkeypatch.setenv("LOGGING__FILE_ENABLED", "false")
    monkeypatch.setenv("LOGGING__FILE_PATH", "var/log/app.log")

    settings = Settings()

    assert settings.postgres.host == "db"
    assert settings.postgres.port == _TEST_PORT
    assert settings.postgres.user == "svc"
    assert settings.postgres.password == "secret"
    assert settings.postgres.db == "dmc"
    assert settings.redis.url == "redis://cache:6380/1"
    assert settings.logging.level == "DEBUG"
    assert settings.logging.format == "json"
    assert settings.logging.file_enabled is False
    assert settings.logging.file_path == Path("var/log/app.log")


def test_logging_level_accepts_lowercase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logging level names are accepted case-insensitively ("info" == "INFO")."""
    monkeypatch.setenv("LOGGING__LEVEL", "debug")

    settings = Settings()

    assert settings.logging.level == "DEBUG"


def test_postgres_url_quotes_special_characters() -> None:
    """Postgres.url builds an async SQLAlchemy URL with quoted credentials."""

    postgres = Postgres(
        host="db", port=_TEST_PORT, user="svc", password="p@ss w:rd", db="dmc"
    )

    assert postgres.url == "postgresql+asyncpg://svc:p%40ss+w%3Ard@db:5433/dmc"


_EURO_BASE_URL = "https://eurorouter.example/v1"


def _set_eurorouter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM__PRIMARY_PROVIDER")
    monkeypatch.delenv("LLM__FALLBACK_ENABLED")
    monkeypatch.setenv("LLM__EUROROUTER__BASE_URL", _EURO_BASE_URL)
    monkeypatch.setenv("LLM__EUROROUTER__MODEL", "euro-model")
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", " key-one, ,key-two ")


def test_llm_defaults_to_eurorouter_with_ollama_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_eurorouter_env(monkeypatch)

    llm = Settings().llm

    assert llm.provider_chain == ["eurorouter", "ollama"]
    assert llm.eurorouter.base_url == _EURO_BASE_URL
    assert [k.get_secret_value() for k in llm.eurorouter.api_keys] == [
        "key-one",
        "key-two",
    ]
    assert llm.ollama.base_url == "http://ollama:11434"


@pytest.mark.parametrize(
    "missing",
    [
        "LLM__EUROROUTER__API_KEYS",
        "LLM__EUROROUTER__BASE_URL",
        "LLM__EUROROUTER__MODEL",
    ],
)
@pytest.mark.parametrize("primary", ["eurorouter", "ollama"])
def test_eurorouter_in_chain_requires_its_settings(
    monkeypatch: pytest.MonkeyPatch, missing: str, primary: str
) -> None:
    _set_eurorouter_env(monkeypatch)
    monkeypatch.setenv("LLM__PRIMARY_PROVIDER", primary)
    monkeypatch.setenv(missing, "")

    with pytest.raises(ValidationError) as error:
        Settings()

    assert "key-one" not in str(error.value)


@pytest.mark.parametrize("missing", ["LLM__OLLAMA__MODEL", "LLM__OLLAMA__BASE_URL"])
def test_ollama_in_chain_requires_its_settings(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv(missing, "")

    with pytest.raises(ValidationError, match=missing):
        Settings()


def test_ollama_settings_not_required_when_out_of_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_eurorouter_env(monkeypatch)
    monkeypatch.setenv("LLM__FALLBACK_ENABLED", "false")
    monkeypatch.setenv("LLM__OLLAMA__MODEL", "")

    assert Settings().llm.provider_chain == ["eurorouter"]


def test_eurorouter_base_url_must_be_https(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_eurorouter_env(monkeypatch)
    monkeypatch.setenv("LLM__EUROROUTER__BASE_URL", "http://eurorouter.example/v1")

    with pytest.raises(ValidationError, match="https"):
        Settings()


def test_llm_limits_have_spec_defaults() -> None:
    llm = Settings().llm

    assert (llm.context_window, llm.max_input_tokens, llm.max_output_tokens) == (
        16384,
        10000,
        2000,
    )
    assert (
        llm.run_max_requests,
        llm.run_max_input_tokens,
        llm.run_max_output_tokens,
        llm.run_deadline_s,
        llm.max_chunks,
    ) == (8, 80000, 16000, 600, 5)
    assert (llm.prompt_version, llm.request_timeout_s) == ("v1", 120)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LLM__CONTEXT_WINDOW", "8192"),
        ("LLM__MAX_INPUT_TOKENS", "15000"),
        ("LLM__MAX_OUTPUT_TOKENS", "7000"),
    ],
)
def test_llm_token_limits_must_fit_context_window(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()
