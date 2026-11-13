"""Offline Eurorouter through real HTTP auth, Redis admission and coordinator."""

import asyncio
import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any, Literal

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse
from test_rule_simulations_access import SimulationScope
from test_rule_simulations_body import (
    isolate_stream_from_sentry,  # noqa: F401 -- module-local stream isolation
)
from test_rule_simulations_integration import (
    CODE,
    DIFF,
    DIFF_FINDINGS,
    FINDING,
    RULES,
    api_errors,  # noqa: F401 -- dependency of the module-local auth_app fixture
    http_admission,  # noqa: F401 -- real disposable Redis admission fixture
    no_simulation_writes,  # noqa: F401 -- autouse guard stays local to this module
)
from test_rule_simulations_integration import (
    auth_app as auth_app,  # noqa: PLC0414 -- pytest fixture export
)
from test_rule_simulations_integration import (
    execution_budget as execution_budget,  # noqa: PLC0414 -- pytest fixture export
)
from test_rule_simulations_integration import (
    shared_coordinator as shared_coordinator,  # noqa: PLC0414 -- pytest fixture export
)

from app.api.rule_simulations import (
    SimulationExecutor,
    get_simulation_coordinator,
    get_simulation_executor,
)
from app.application.simulation_execution import SimulationExecutionCoordinator
from app.llm.openai_compatible import OpenAICompatibleClient
from app.review.simulation import CodeSimulationService

pytest_plugins = ("test_rule_simulations_access",)


class OfflineProvider:
    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.answer = '{"findings":[]}'
        self.failure: Literal["read-timeout", "deadline"] | None = None
        self.cancelled = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        assert len(self.sent) == 1, "no second inference is permitted"
        assert str(request.url) == "https://api.eurouter.ai/api/v1/chat/completions"
        assert json.loads(request.content)["model"] == "mistral-small-4"
        if self.failure == "read-timeout":
            raise httpx.ReadTimeout("private-provider-response-sentinel")
        if self.failure == "deadline":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return httpx.Response(
            200, json={"choices": [{"message": {"content": self.answer}}]}
        )


@pytest.fixture
async def eurorouter(
    auth_app: FastAPI,
    shared_coordinator: SimulationExecutionCoordinator[JSONResponse],
) -> AsyncIterator[OfflineProvider]:
    provider = OfflineProvider()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.handle), trust_env=False
    ) as http:
        # Structural assignment also checks the HTTP protocol with mypy. There
        # is no wrapper between the coordinator's deadline and the service.
        executor: SimulationExecutor = CodeSimulationService(
            OpenAICompatibleClient(
                name="eurorouter",
                http=http,
                base_url="https://api.eurouter.ai/api/v1",
                model="mistral-small-4",
                api_key="offline-http-synthetic-key",
            )
        )
        auth_app.dependency_overrides[get_simulation_executor] = lambda: executor
        try:
            yield provider
        finally:
            assert await shared_coordinator.aclose()
            auth_app.dependency_overrides.pop(get_simulation_executor, None)


@pytest.mark.parametrize(
    ("input_data", "findings"),
    [(CODE, [FINDING]), (DIFF, DIFF_FINDINGS)],
    ids=["code", "diff"],
)
async def test_direct_service_returns_http_200_and_duplicate_does_not_infer(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    eurorouter: OfflineProvider,
    input_data: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    eurorouter.answer = json.dumps({"findings": findings})
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": input_data, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"findings": findings, "rejected_count": 0}
    assert len(eurorouter.sent) == 1
    duplicate = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": input_data, "rules": RULES},
    )
    assert duplicate.status_code == 503  # noqa: PLR2004 -- no result replay
    assert len(eurorouter.sent) == 1


@pytest.mark.parametrize("input_data", [CODE, DIFF], ids=["code", "diff"])
@pytest.mark.parametrize("failure", ["read-timeout", "deadline"])
@pytest.mark.parametrize("execution_budget", [0.1])
async def test_provider_or_coordinator_timeout_returns_canonical_503(  # noqa: PLR0913, PLR0917 -- HTTP boundary fixtures
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    eurorouter: OfflineProvider,
    input_data: dict[str, Any],
    failure: Literal["read-timeout", "deadline"],
    execution_budget: float,
) -> None:
    eurorouter.failure = failure
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": input_data, "rules": RULES},
    )
    assert response.status_code == 503  # noqa: PLR2004 -- canonical unavailable
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert len(eurorouter.sent) == 1
    assert all(
        0 < timeout <= execution_budget
        for timeout in eurorouter.sent[0].extensions["timeout"].values()
    )
    if failure == "deadline":
        await asyncio.wait_for(eurorouter.cancelled.wait(), 0.5)


async def test_normal_runtime_stays_disabled_with_canonical_503(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
) -> None:
    assert await get_simulation_executor() is None
    assert await get_simulation_coordinator() is None
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": CODE, "rules": RULES},
    )
    assert response.status_code == 503  # noqa: PLR2004 -- disabled runtime
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
