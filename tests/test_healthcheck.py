"""Liveness healthcheck reports the build commit and stays free of the database."""

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


async def test_healthcheck_returns_commit_from_git_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SHA", "abc123def456")
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthcheck")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok", "commit": "abc123def456"}


async def test_healthcheck_commit_is_unknown_when_git_sha_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthcheck")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok", "commit": "unknown"}


async def test_healthcheck_honors_settings_override() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(git_sha="override-sha")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthcheck")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok", "commit": "override-sha"}


async def test_healthcheck_commit_is_unknown_when_git_sha_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SHA", "   ")
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthcheck")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["commit"] == "unknown"


def test_openapi_health_schema_requires_commit() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    health = spec["components"]["schemas"]["Health"]

    assert health["additionalProperties"] is False
    assert set(health["required"]) >= {"status", "commit"}
    assert health["properties"]["status"]["const"] == "ok"
    assert health["properties"]["commit"]["type"] == "string"
    assert health["properties"]["commit"]["minLength"] == 1


def test_dockerfile_passes_git_sha_into_the_image() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "ARG GIT_SHA=unknown" in dockerfile
    assert "ENV GIT_SHA=$GIT_SHA" in dockerfile


def test_ci_docker_builds_pass_git_sha() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "api-ci.yml"
    ).read_text(encoding="utf-8")
    image_build, publish_build = [
        line for line in workflow.splitlines() if "docker build" in line
    ]
    assert "--build-arg GIT_SHA=" in image_build
    assert "--build-arg GIT_SHA=" in publish_build
    assert (
        "--build-arg GIT_SHA=${{ github.event.pull_request.head.sha || github.sha }}"
        in image_build
    )
    assert "--build-arg GIT_SHA=${{ github.sha }}" in publish_build
