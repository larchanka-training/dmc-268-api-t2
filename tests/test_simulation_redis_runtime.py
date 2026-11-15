"""Dedicated admission configuration at the application lifespan boundary."""

# ruff: noqa: PLR2004 -- HTTP status literals are acceptance criteria

from typing import Any

import httpx
import pytest
from redis.asyncio import ConnectionPool
from test_simulation_runtime import (
    enable_configuration,
    isolated_runtime_environment,  # noqa: F401 -- explicitly local fixture
)

from app.core.config import get_settings
from app.main import create_app


async def test_enabled_without_dedicated_url_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)
    monkeypatch.delenv("SIMULATION__REDIS_URL", raising=False)
    app = create_app()
    async with app.router.lifespan_context(app):
        runtime = app.state.simulation_runtime
        assert runtime.coordinator is None
        assert runtime.executor is None
        assert runtime.http is None
        assert runtime.admission is None
        assert get_settings().simulation.redis_url is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://test"
        ) as browser:
            assert (await browser.get("/healthcheck")).status_code == 200


@pytest.mark.parametrize(
    "dedicated",
    [
        "redis://redis:6379/0",
        "redis://other:synthetic-password@REDIS.:6379/9",
        "rediss://redis/2",
        "redis://%72edis:6379/1",
        "redis://simulation-redis:6379/0?host=redis",
        "not-a-redis-url-with-synthetic-password",
        "",
    ],
)
async def test_shared_or_invalid_dedicated_url_fails_closed_without_diagnostics(
    dedicated: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enable_configuration(monkeypatch)
    monkeypatch.setenv("REDIS__URL", "redis://redis:6379/0")
    monkeypatch.setenv("SIMULATION__REDIS_URL", dedicated)
    app = create_app()
    async with app.router.lifespan_context(app):
        runtime = app.state.simulation_runtime
        assert runtime.coordinator is None
        assert runtime.http is None
        assert runtime.admission is None
    assert "synthetic-password" not in caplog.text
    assert "synthetic-password" not in repr(get_settings().simulation)


async def test_runtime_builds_one_dedicated_pool_without_shared_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    enable_configuration(monkeypatch)
    monkeypatch.setenv("REDIS__URL", "redis://review-redis:6379/0")
    monkeypatch.setenv("SIMULATION__REDIS_URL", "redis://simulation-redis:6379/0")
    hosts: list[tuple[str, int, int]] = []
    original = ConnectionPool.__init__

    def observe(self: ConnectionPool, **kwargs: Any) -> None:
        hosts.append((kwargs["host"], kwargs.get("port", 6379), kwargs["db"]))
        original(self, **kwargs)

    monkeypatch.setattr(ConnectionPool, "__init__", observe)
    app = create_app()
    async with app.router.lifespan_context(app):
        runtime = app.state.simulation_runtime
        assert runtime.accepting
        assert runtime.coordinator is not None
        assert runtime.admission is not None
        assert hosts == [("simulation-redis", 6379, 0)]


@pytest.mark.parametrize("url", [None, "redis://redis:6379/0", "invalid-secret-url"])
async def test_disabled_runtime_never_allocates_clients(
    url: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:

    if url is not None:
        monkeypatch.setenv("SIMULATION__REDIS_URL", url)
    monkeypatch.setenv("SIMULATION__ENABLED", "false")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("disabled runtime created a client")

    monkeypatch.setattr(ConnectionPool, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.simulation_runtime.coordinator is None
        assert app.state.simulation_runtime.http is None


@pytest.mark.parametrize("alias", ["[::ffff:127.0.0.1]", "127.1", "2130706433"])
async def test_shared_loopback_alias_cannot_bypass_instance_isolation(
    alias: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)
    monkeypatch.setenv("REDIS__URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("SIMULATION__REDIS_URL", f"redis://{alias}:6379/9")
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.simulation_runtime.coordinator is None
        assert app.state.simulation_runtime.http is None


@pytest.mark.parametrize("reverse", [False, True])
async def test_mapped_remote_ipv4_alias_cannot_bypass_instance_isolation(
    reverse: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_configuration(monkeypatch)
    hosts = ["192.0.2.1", "[::ffff:192.0.2.1]"]
    if reverse:
        hosts.reverse()
    monkeypatch.setenv("REDIS__URL", f"redis://{hosts[0]}:6379/0")
    monkeypatch.setenv("SIMULATION__REDIS_URL", f"redis://{hosts[1]}:6379/9")
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.simulation_runtime.coordinator is None
        assert app.state.simulation_runtime.http is None
