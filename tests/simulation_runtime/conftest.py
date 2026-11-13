"""Local-only fixture composition; never register foreign integration modules."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.types import Message
from test_auth_api import (
    auth_app,  # noqa: F401 -- explicit local fixtures
    browser,  # noqa: F401
    github_requests,  # noqa: F401
    provider,  # noqa: F401
)
from test_auth_api import (
    auth_settings as base_auth_settings,  # noqa: F401
)
from test_postgres_integration import postgres  # noqa: F401
from test_rule_simulations_access import simulation_scope  # noqa: F401
from test_simulation_runtime import (
    enable_configuration,
    isolated_runtime_environment,  # noqa: F401
)

from app.adapters.simulation_admission import STATE_KEY
from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def runtime_environment(
    isolated_runtime_environment: None,  # noqa: F811 -- explicit local fixture injection
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)
    monkeypatch.setenv("REDIS__URL", os.environ["DMC268_TEST_REDIS_URL"])


@pytest.fixture
def auth_settings(base_auth_settings: Settings) -> Settings:  # noqa: F811 -- fixture injection
    return get_settings().model_copy(update={"auth": base_auth_settings.auth})


@dataclass
class ProviderIO:
    requests: list[httpx.Request] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    finish: asyncio.Event | None = None
    ignore_cancellation: bool = False
    failure: Exception | None = None
    status: int = 200
    output: dict[str, Any] = field(default_factory=lambda: {"findings": []})

    async def dispatch(self, request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.eurouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-first-key"
        self.requests.append(request)
        self.entered.set()
        if self.finish is not None:
            while not self.finish.is_set():
                try:
                    await self.finish.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancellation:
                        raise
        if self.failure is not None:
            raise self.failure
        return httpx.Response(
            self.status,
            json={
                "choices": [{"message": {"content": json.dumps(self.output)}}],
            },
        )


@pytest.fixture
def provider_io() -> ProviderIO:
    return ProviderIO()


@pytest.fixture
def no_external_http(
    monkeypatch: pytest.MonkeyPatch,
    provider: dict[str, httpx.Response],  # noqa: F811 -- local fixture injection
    github_requests: list[httpx.Request],  # noqa: F811 -- local fixture injection
    provider_io: ProviderIO,
) -> None:
    async def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.eurouter.ai":
            return await provider_io.dispatch(request)
        assert request.url.host == "api.github.com"
        assert request.url.path.startswith("/repositories/")
        assert request.url.path in provider
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-user-secret"
        github_requests.append(request)
        return provider[request.url.path]

    transport = httpx.MockTransport(dispatch)

    async def handle(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)


@pytest.fixture
async def admission_state() -> AsyncIterator[Redis]:
    async with Redis.from_url(
        os.environ["DMC268_TEST_REDIS_URL"], decode_responses=True
    ) as client:
        await client.delete(STATE_KEY)
        try:
            yield client
        finally:
            await client.delete(STATE_KEY)


async def provision(client: Redis, app: FastAPI) -> None:
    # Test-only external provisioning. Production must never create this state.
    await client.set(
        STATE_KEY,
        json.dumps(
            {
                "schema_version": 3,
                "lease": None,
                "admissions": [],
                "consumed_keys": [],
                "hmac_config_id": app.state.simulation_runtime.admission.hmac_config_id,
            }
        ),
    )


@asynccontextmanager
async def protocol_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Drive actual ASGI startup/shutdown messages without another dependency."""
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    outgoing: asyncio.Queue[Message] = asyncio.Queue()
    server = asyncio.create_task(
        app(
            {
                "type": "lifespan",
                "asgi": {"version": "3.0", "spec_version": "2.0"},
                "state": {},
            },
            incoming.get,
            outgoing.put,
        )
    )
    await incoming.put({"type": "lifespan.startup"})
    assert (await asyncio.wait_for(outgoing.get(), 2))[
        "type"
    ] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await incoming.put({"type": "lifespan.shutdown"})
        assert (await asyncio.wait_for(outgoing.get(), 3))[
            "type"
        ] == "lifespan.shutdown.complete"
        await asyncio.wait_for(server, 1)
