"""Application-owned Playground resources at the actual ASGI lifespan seam."""

# ruff: noqa: PLR2004 -- byte caps, timeouts and HTTP statuses are literal AC values

import asyncio
import base64
import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends
from redis.asyncio import ConnectionPool, Redis
from simulation_redis_fixtures import (
    dedicated_simulation_redis_url,  # noqa: F401 -- explicit local fixture
)
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.adapters.simulation_admission import STATE_KEY
from app.api.rule_simulations import (
    SimulationCodeInput,
    SimulationExecutor,
    SimulationRules,
    get_simulation_coordinator,
    get_simulation_executor,
)
from app.application.simulation_execution import SimulationExecutionCoordinator
from app.core.config import get_settings
from app.llm.openai_compatible import OpenAICompatibleClient
from app.main import create_app


@pytest.fixture(autouse=True)
def isolated_runtime_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(
            ("LLM__", "SIMULATION__", "ANALYSIS__", "METRICS__", "SENTRY__")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LOGGING__FILE_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_disabled_lifespan_owns_unavailable_runtime_without_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ASGI test client is a test resource, created before constructor guards.
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://test"
    ) as browser:

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("disabled application touched simulation resources")

        monkeypatch.setattr(Path, "read_bytes", forbidden)
        environment_get = os.environ.get

        def guarded_get(name: str, default: Any = None) -> Any:
            if name.startswith(("LLM__", "SIMULATION__")):
                forbidden()
            return environment_get(name, default)

        monkeypatch.setattr(os.environ, "get", guarded_get)
        monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
        monkeypatch.setattr(Redis, "__init__", forbidden)
        monkeypatch.setattr(OpenAICompatibleClient, "__init__", forbidden)
        # create_app itself must not create simulation resources either.
        another = create_app()
        async with app.router.lifespan_context(app):
            runtime = app.state.simulation_runtime
            assert runtime.executor is None
            assert runtime.coordinator is None
            assert (await browser.get("/healthcheck")).status_code == 200
            assert (await browser.get("/")).status_code == 200
        async with another.router.lifespan_context(another):
            assert another.state.simulation_runtime is not runtime


MANIFEST = Path(__file__).resolve().parents[1] / "deploy/analysis-runtime.staging.json"


def enable_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMULATION__ENABLED", "true")
    monkeypatch.setenv(
        "SIMULATION__REDIS_URL",
        os.environ.get(
            "DMC268_TEST_SIMULATION_REDIS_URL", "redis://simulation-redis:6379/0"
        ),
    )
    monkeypatch.setenv(
        "SIMULATION__HMAC_SECRET_B64", base64.b64encode(b"s" * 32).decode()
    )
    monkeypatch.setenv("SIMULATION__MANIFEST_PATH", str(MANIFEST))
    monkeypatch.setenv("LLM__FALLBACK_ENABLED", "false")
    monkeypatch.setenv("LLM__EUROROUTER__BASE_URL", "https://api.eurouter.ai/api/v1")
    monkeypatch.setenv("LLM__EUROROUTER__MODEL", "mistral-small-4")
    monkeypatch.setenv(
        "LLM__EUROROUTER__API_KEYS", "synthetic-first-key,synthetic-second-key"
    )


async def test_enabled_lifespan_prepares_explicit_trusted_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)
    app = create_app()
    async with app.router.lifespan_context(app):
        config = app.state.simulation_runtime.configuration
        assert config is not None
        assert (config.provider, config.model) == ("eurorouter", "mistral-small-4")
        assert config.api_key.get_secret_value() == "synthetic-first-key"
        assert (config.max_input_tokens, config.max_output_tokens) == (8192, 2000)
        assert config.execution_timeout_s == 30.0


async def test_invalid_credentials_keep_healthcheck_up_and_runtime_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "invalid key")
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.simulation_runtime.configuration is None
        assert app.state.simulation_runtime.executor is None
        assert app.state.simulation_runtime.coordinator is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://test"
        ) as browser:
            assert (await browser.get("/healthcheck")).status_code == 200


async def test_missing_stable_secret_keeps_runtime_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)
    monkeypatch.delenv("SIMULATION__HMAC_SECRET_B64")
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.simulation_runtime.configuration is None
        assert app.state.simulation_runtime.coordinator is None


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "!invalid!",
        "é",
        base64.b64encode(b"s" * 31).decode(),
        base64.b64encode(b"s" * 1025).decode(),
        base64.b64encode(b"s" * 32).decode() + "\n",
    ],
)
async def test_invalid_stable_secret_keeps_runtime_unavailable(
    monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    enable_configuration(monkeypatch)
    monkeypatch.setenv("SIMULATION__HMAC_SECRET_B64", secret)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.simulation_runtime.configuration is None
        assert app.state.simulation_runtime.executor is None
        assert app.state.simulation_runtime.coordinator is None


async def test_valid_lifespan_composes_one_direct_provider_after_redis_admission(
    monkeypatch: pytest.MonkeyPatch,
    dedicated_simulation_redis_url: str,  # noqa: F811 -- fixture injection
) -> None:

    enable_configuration(monkeypatch)
    redis_url = dedicated_simulation_redis_url
    monkeypatch.setenv("SIMULATION__REDIS_URL", redis_url)
    monkeypatch.setenv("REDIS__URL", os.environ["DMC268_TEST_REDIS_URL"])
    monkeypatch.setenv("HTTPS_PROXY", "http://unapproved-proxy.invalid:9999")
    requests: list[httpx.Request] = []

    async def dispatch(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://api.eurouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-first-key"
        body = json.loads(request.content)
        assert body["model"] == "mistral-small-4"
        assert body["max_tokens"] == 2000
        assert request.extensions["timeout"]["read"] <= 30.0
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"findings":[]}'}}]}
        )

    transport = httpx.MockTransport(dispatch)

    async def handle(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    app = create_app()
    async with Redis.from_url(redis_url, decode_responses=True) as redis:
        await redis.delete(STATE_KEY)
        async with app.router.lifespan_context(app):
            runtime = app.state.simulation_runtime
            assert runtime.executor is not None
            assert runtime.coordinator is not None
            try:
                await redis.set(
                    STATE_KEY,
                    json.dumps(
                        {
                            "schema_version": 3,
                            "lease": None,
                            "admissions": [],
                            "consumed_keys": [],
                            "hmac_config_id": runtime.admission.hmac_config_id,
                        }
                    ),
                )

                async def execute(deadline: float) -> JSONResponse:
                    result = await runtime.executor.simulate(
                        SimulationCodeInput(
                            kind="code", path="example.py", source="print('ok')"
                        ),
                        SimulationRules(
                            format="rules-md/1",
                            markdown="# Security\n## SEC-1: No eval\nAvoid eval.",
                        ),
                        deadline=deadline,
                    )
                    return JSONResponse(result.model_dump())

                result = await runtime.coordinator.execute(
                    uuid4(), uuid4(), b"composition", execute
                )
                assert result.status == "SUCCESS"
                assert len(requests) == 1
                assert runtime.http.trust_env is False
                assert runtime.http.follow_redirects is False
            finally:
                await runtime.coordinator.aclose()
                await runtime.admission.aclose()
                await runtime.http.aclose()
                await redis.delete(STATE_KEY)


async def test_http_dependencies_borrow_same_application_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    enable_configuration(monkeypatch)
    app = create_app()
    borrowed: list[tuple[object, object]] = []

    @app.get("/test-runtime")
    async def probe(
        executor: Annotated[
            SimulationExecutor | None, Depends(get_simulation_executor)
        ],
        coordinator: Annotated[
            SimulationExecutionCoordinator[JSONResponse] | None,
            Depends(get_simulation_coordinator),
        ],
    ) -> dict[str, bool]:
        borrowed.append((executor, coordinator))
        return {"available": executor is not None and coordinator is not None}

    async with app.router.lifespan_context(app):
        runtime = app.state.simulation_runtime
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="https://test"
            ) as browser:
                for _ in range(2):
                    assert (await browser.get("/test-runtime")).json() == {
                        "available": True
                    }
            assert borrowed == [(runtime.executor, runtime.coordinator)] * 2
        finally:
            await runtime.coordinator.aclose()
            await runtime.admission.aclose()
            await runtime.http.aclose()


async def test_lifespan_closes_idle_resources_and_disables_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    enable_configuration(monkeypatch)
    app = create_app()
    async with app.router.lifespan_context(app):
        runtime = app.state.simulation_runtime
        http = runtime.http
        assert not http.is_closed
    assert http.is_closed
    request = Request({"type": "http", "app": app})
    assert await get_simulation_executor(request) is None
    assert await get_simulation_coordinator(request) is None


async def test_partial_client_initialization_fails_closed_and_reclaims_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)

    def fail_pool(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic pool initialization failure")

    monkeypatch.setattr(ConnectionPool, "__init__", fail_pool)
    clients: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient.__init__

    def observe(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        clients.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", observe)
    app = create_app()
    async with app.router.lifespan_context(app):
        runtime = app.state.simulation_runtime
        assert runtime.executor is None
        assert runtime.coordinator is None
        assert len(clients) == 1
        assert clients[0].is_closed


async def test_shutdown_disables_simulation_before_queue_metrics_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)
    monkeypatch.setenv("REDIS__URL", os.environ["DMC268_TEST_REDIS_URL"])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    monkeypatch.setenv("METRICS__ENABLED", "true")
    monkeypatch.setenv("METRICS__PORT", str(port))
    closing = asyncio.Event()
    finish = asyncio.Event()
    original = Redis.aclose

    async def hold_close(client: Redis, *args: Any, **kwargs: Any) -> None:
        closing.set()
        await finish.wait()
        await original(client, *args, **kwargs)

    monkeypatch.setattr(Redis, "aclose", hold_close)
    app = create_app()
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    runtime = app.state.simulation_runtime
    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(closing.wait(), 2)
        assert runtime.accepting is False
    finally:
        finish.set()
        await asyncio.wait_for(shutdown, 4)
