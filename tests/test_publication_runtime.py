"""Write credential configuration through the public publication runtime."""

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.application.github_ports import GithubRequest
from app.core.config import Settings
from app.core.publication_runtime import github_publisher_connection

HEAD = "a" * 40
BASE = "b" * 40


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("GITHUB__TOKEN", "GITHUB__WRITE_TOKEN", "GITHUB__BOT_USER_ID"):
        monkeypatch.delenv(key, raising=False)


async def test_publication_connection_uses_only_redacted_write_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-RUNTIME-01: publisher sends its own write token, never the read token."""
    monkeypatch.setenv("GITHUB__TOKEN", "read-token-private")
    monkeypatch.setenv("GITHUB__WRITE_TOKEN", "write-token-private")
    monkeypatch.setenv("GITHUB__BOT_USER_ID", "9002")
    settings = Settings()
    observed: list[tuple[str, str, str, str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(
            (
                request.url.host,
                request.headers["Authorization"],
                request.headers["X-GitHub-Api-Version"],
                request.headers["User-Agent"],
                request.extensions.get("timeout"),
            )
        )
        return httpx.Response(
            200,
            json={
                "state": "open",
                "number": 42,
                "title": "Review",
                "body": None,
                "head": {"sha": HEAD, "ref": "topic", "repo": {"id": 101}},
                "base": {"sha": BASE, "ref": "main", "repo": {"id": 100}},
            },
        )

    async with github_publisher_connection(
        settings,
        transport=httpx.MockTransport(respond),
    ) as github:
        await github.check_pull(GithubRequest("team", "demo", "100", 42, HEAD, BASE))

    assert observed == [
        (
            "api.github.com",
            "Bearer write-token-private",
            "2022-11-28",
            "dmc-268-api-t2",
            {"connect": 15, "read": 15, "write": 15, "pool": 15},
        )
    ]
    assert settings.github.bot_user_id == "9002"
    assert "read-token-private" not in repr(settings)
    assert "write-token-private" not in repr(settings)
    assert "write-token-private" not in settings.model_dump_json()


@pytest.mark.parametrize("missing", ["GITHUB__WRITE_TOKEN", "GITHUB__BOT_USER_ID"])
async def test_publication_connection_fails_closed_before_http_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """AC-RUNTIME-02: a configured read token cannot enable publication."""
    monkeypatch.setenv("GITHUB__TOKEN", "read-token-private")
    monkeypatch.setenv("GITHUB__WRITE_TOKEN", "write-token-private")
    monkeypatch.setenv("GITHUB__BOT_USER_ID", "9002")
    monkeypatch.setenv(missing, "")
    settings = Settings()
    observed: list[str] = []
    entered = False

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(200, json=[])

    with pytest.raises(ValueError, match="GitHub publication credentials") as error:
        async with github_publisher_connection(
            settings,
            transport=httpx.MockTransport(respond),
        ) as github:
            entered = True
            await github.list_reviews(
                GithubRequest("team", "demo", "100", 42, HEAD, BASE)
            )

    assert entered is False
    assert observed == []
    assert "read-token-private" not in str(error.value)
    assert "write-token-private" not in str(error.value)


@pytest.mark.parametrize(
    "bot_id", ["0", "-2", "9002/other", "abc", "\uff19\uff10\uff10\uff12", "09002"]
)
def test_publication_settings_reject_noncanonical_bot_identity(
    monkeypatch: pytest.MonkeyPatch,
    bot_id: str,
) -> None:
    """AC-RUNTIME-03: configured bot identity must match a positive GitHub ID."""
    monkeypatch.setenv("GITHUB__WRITE_TOKEN", "write-token-private")
    monkeypatch.setenv("GITHUB__BOT_USER_ID", bot_id)

    with pytest.raises(ValidationError, match="GITHUB__BOT_USER_ID") as error:
        Settings()

    assert "write-token-private" not in str(error.value)
