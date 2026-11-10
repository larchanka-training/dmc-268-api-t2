"""Test-only execution through the real session/CSRF/PostgreSQL HTTP boundary."""

import json
import traceback
from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from starlette.responses import JSONResponse
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import SimulationScope, simulation_state

from app.api.errors import ApiError, api_error_response
from app.api.rule_simulations import get_simulation_executor
from app.db.base import Base
from app.llm.client import BaseLLMClient, LLMCallResult, LLMRequest
from app.llm.errors import (
    LLMGatewayError,
    SchemaValidationError,
    TransientProviderError,
)
from app.main import create_app
from app.review.simulation import CodeSimulationService

pytest_plugins = ("test_rule_simulations_access",)

RULES = {
    "format": "rules-md/1",
    "markdown": "# Security\n## SEC-1: No eval\nprivate_rules_sentinel\n",
}
CODE = {
    "kind": "code",
    "path": "playground/example.py",
    "source": "# header\neval(private_source_sentinel)\nprint('done')\n",
}
DIFF = {
    "kind": "diff",
    "unified_diff": (
        "diff --git a/before.py b/after.py\n"
        "similarity index 50%\nrename from before.py\nrename to after.py\n"
        "--- a/before.py\n+++ b/after.py\n@@ -8,2 +20,2 @@\n"
        "-old_eval(value)\n-old_tail()\n+eval(value)\n+new_tail()\n"
    ),
}
FINDING = {
    "path": "playground/example.py",
    "start_line": 2,
    "end_line": 3,
    "rule_ref": "SEC-1",
    "title": "Unsafe evaluation",
    "explanation": "User input is executed.",
    "evidence": "eval(private_source_sentinel)",
}
DIFF_FINDINGS = [
    {
        "path": "before.py",
        "side": "old",
        "start_line": 8,
        "end_line": 9,
        "rule_ref": "SEC-1",
        "title": "Unsafe old evaluation",
        "explanation": "Old input is executed.",
        "evidence": "old_eval(value)",
    },
    {
        "path": "after.py",
        "side": "new",
        "start_line": 20,
        "end_line": 21,
        "rule_ref": "SEC-1",
        "title": "Unsafe new evaluation",
        "explanation": "New input is executed.",
        "evidence": "eval(value)",
    },
]


@pytest_asyncio.fixture(autouse=True)
async def no_simulation_writes(
    postgres: PostgresHarness, simulation_scope: SimulationScope
) -> AsyncIterator[None]:
    # A side-effect invariant, separate from HTTP outcome assertions. Cover every
    # mapped table, including idempotency, findings, auth and accounting tables.
    async def snapshot() -> list[list[object]]:
        async with postgres.engine.connect() as connection:
            return [
                list(
                    (
                        await connection.execute(
                            select(table).order_by(*table.primary_key)
                        )
                    )
                    .tuples()
                    .all()
                )
                for table in Base.metadata.tables.values()
            ]

    before = await snapshot()
    yield
    assert await snapshot() == before


@pytest.fixture
def api_errors() -> list[Exception]:
    return []


@pytest_asyncio.fixture
async def auth_app(api_errors: list[Exception]) -> FastAPI:
    # Register the public error observer before browser/login can build ASGI.
    app = create_app()

    async def observe_error(request: Request, error: Exception) -> JSONResponse:
        api_errors.append(error)
        return await api_error_response(request, error)

    app.add_exception_handler(ApiError, observe_error)
    return app


class FakeLLM(BaseLLMClient):
    name = "http-simulation-test"

    def __init__(self) -> None:
        self.answer = json.dumps({"findings": [FINDING]})
        self.requests: list[LLMRequest] = []
        self.failure: LLMGatewayError | None = None

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, response_model: type[T]
    ) -> LLMCallResult[T]:
        assert not self.requests, "simulation must not repeat a model call"
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        try:
            parsed = response_model.model_validate_json(self.answer)
        except ValidationError:
            raise SchemaValidationError([], self.answer) from None
        return LLMCallResult(
            parsed=parsed,
            raw_text_len=len(self.answer),
            input_tokens=None,
            output_tokens=None,
            model="deterministic-test",
            model_digest=None,
            provider=self.name,
            latency_ms=0,
        )


@pytest.fixture
def injected_llm(auth_app: FastAPI) -> Iterator[FakeLLM]:
    fake = FakeLLM()
    service = CodeSimulationService(fake)
    auth_app.dependency_overrides[get_simulation_executor] = lambda: service
    try:
        yield fake
    finally:
        auth_app.dependency_overrides.pop(get_simulation_executor, None)


async def test_validated_code_returns_the_verified_service_result(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    injected_llm: FakeLLM,
) -> None:
    before = await simulation_state(postgres)
    github_before = len(github_requests)
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": CODE, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"findings": [FINDING], "rejected_count": 0}
    assert response.headers["X-Request-Id"]
    assert "Set-Cookie" not in response.headers
    assert len(injected_llm.requests) == 1
    prompt = json.loads(injected_llm.requests[0].messages[1].content)
    assert prompt == {
        "path": "playground/example.py",
        "source_lines": [
            "# header",
            "eval(private_source_sentinel)",
            "print('done')",
        ],
        "rules": ["[SEC-1] Security: No eval\nprivate_rules_sentinel"],
    }
    assert [request.url.path for request in github_requests[github_before:]] == [
        f"/repositories/{simulation_scope.external_id}"
    ]
    assert await simulation_state(postgres) == before


@pytest.mark.parametrize("input_data", [CODE, DIFF], ids=["code", "diff"])
@pytest.mark.parametrize("outcome", ["invalid-json", "provider-failure"])
async def test_model_failure_returns_only_a_safe_http_error(  # noqa: PLR0913, PLR0917 -- real HTTP boundary and diagnostic fixtures
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    injected_llm: FakeLLM,
    api_errors: list[Exception],
    caplog: pytest.LogCaptureFixture,
    input_data: dict[str, str],
    outcome: str,
) -> None:
    injected_llm.answer = (
        "not JSON: private_source_sentinel private_rules_sentinel "
        "offline-credential-sentinel"
    )
    if outcome == "provider-failure":
        injected_llm.failure = TransientProviderError(injected_llm.answer)
    before = await simulation_state(postgres)
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": input_data, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert len(injected_llm.requests) == 1
    assert len(api_errors) == 1
    assert api_errors[0].__context__ is None
    assert api_errors[0].__cause__ is None
    for private in (
        "private_source_sentinel",
        "private_rules_sentinel",
        "offline-credential-sentinel",
        injected_llm.answer,
    ):
        assert private not in (
            response.text
            + caplog.text
            + repr(vars(api_errors[0]))
            + "".join(traceback.format_exception(api_errors[0]))
        )
    assert await simulation_state(postgres) == before


async def test_diff_preserves_verified_sides_paths_ranges_refs_and_rejected_count(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
) -> None:
    injected_llm.answer = json.dumps(
        {"findings": [*DIFF_FINDINGS, DIFF_FINDINGS[0] | {"rule_ref": "SEC-99"}]}
    )
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": DIFF, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"findings": DIFF_FINDINGS, "rejected_count": 1}
    assert len(injected_llm.requests) == 1


async def test_code_rejected_finding_is_absent_and_count_is_preserved(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    caplog: pytest.LogCaptureFixture,
) -> None:
    injected_llm.answer = json.dumps(
        {"findings": [FINDING, FINDING | {"path": "outside.py"}]}
    )
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": CODE, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"findings": [FINDING], "rejected_count": 1}
    assert len(injected_llm.requests) == 1
    assert "private_source_sentinel" not in caplog.text
    assert "private_rules_sentinel" not in caplog.text


@pytest.mark.parametrize("input_data", [CODE, DIFF], ids=["code", "diff"])
async def test_empty_model_result_has_no_invented_fields(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    input_data: dict[str, str],
) -> None:
    injected_llm.answer = '{"findings":[]}'
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": input_data, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"findings": [], "rejected_count": 0}
    assert len(injected_llm.requests) == 1


@pytest.mark.parametrize(
    "gate",
    [
        "session",
        "origin",
        "csrf",
        "local-access",
        "github-admin",
        "body-limit",
        "content-type",
        "idempotency-key",
        "strict-code",
        "strict-diff",
        "diff-parser",
        "rules",
    ],
)
async def test_rejected_request_never_dispatches_even_with_executor(  # noqa: PLR0913, PLR0917, PLR0912 -- table of existing ordered HTTP gates
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    provider: dict[str, httpx.Response],
    github_requests: list[httpx.Request],
    injected_llm: FakeLLM,
    gate: str,
) -> None:
    headers = dict(simulation_scope.headers)
    repository = simulation_scope.repository
    payload = {"input": CODE, "rules": RULES}
    status, code = HTTPStatus.UNPROCESSABLE_CONTENT, "VALIDATION_FAILED"
    if gate == "session":
        browser.cookies.clear()
        status, code = HTTPStatus.UNAUTHORIZED, "AUTHENTICATION_REQUIRED"
    elif gate == "origin":
        headers["Origin"] = "https://evil.test"
        status, code = HTTPStatus.FORBIDDEN, "ACCESS_DENIED"
    elif gate == "csrf":
        headers["X-CSRF-Token"] = "invalid"
        status, code = HTTPStatus.FORBIDDEN, "ACCESS_DENIED"
    elif gate == "local-access":
        repository = simulation_scope.foreign_repository
        status, code = HTTPStatus.NOT_FOUND, "RESOURCE_NOT_FOUND"
    elif gate == "github-admin":
        provider[f"/repositories/{simulation_scope.external_id}"] = httpx.Response(
            403, text="private-provider-detail"
        )
        status, code = HTTPStatus.FORBIDDEN, "ACCESS_DENIED"
    elif gate == "body-limit":
        status, code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "SIMULATION_INPUT_TOO_LARGE"
    elif gate == "content-type":
        headers["Content-Type"] = "text/plain"
    elif gate == "idempotency-key":
        del headers["Idempotency-Key"]
    elif gate == "strict-code":
        payload = {"input": CODE | {"source": ""}, "rules": RULES}
    elif gate == "strict-diff":
        payload = {"input": DIFF | {"source": "extra"}, "rules": RULES}
    elif gate == "diff-parser":
        payload = {"input": {"kind": "diff", "unified_diff": "broken"}, "rules": RULES}
    elif gate == "rules":
        payload = {"input": CODE, "rules": RULES | {"markdown": ""}}
    body = b" " * 262145 if gate == "body-limit" else json.dumps(payload).encode()
    github_before = len(github_requests)
    response = await browser.post(
        simulation_scope.url(repository), headers=headers, content=body
    )
    assert response.status_code == status
    assert response.json()["code"] == code
    assert injected_llm.requests == []
    if gate in {"session", "origin", "csrf", "local-access"}:
        assert len(github_requests) == github_before
    else:
        assert len(github_requests) == github_before + 1


@pytest.mark.parametrize("input_data", [CODE, DIFF], ids=["code", "diff"])
async def test_normal_application_cannot_enable_execution_over_http(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    input_data: dict[str, str],
) -> None:
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository) + "?fake=true&llm=true",
        headers=simulation_scope.headers
        | {"X-Rule-Playground-Mock": "true", "X-LLM-Provider": "fake"},
        json={"input": input_data, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
