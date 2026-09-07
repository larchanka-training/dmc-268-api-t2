"""Tests for application settings loading."""

from pathlib import Path

import pytest

from app.core.config import Postgres, Settings

_TEST_PORT = 5433


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Settings() from reading the developer's local .env."""
    monkeypatch.chdir(tmp_path)


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
