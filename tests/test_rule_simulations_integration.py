"""Test-only execution through the real session/CSRF/PostgreSQL HTTP boundary."""

import asyncio
import json
import logging
import os
import traceback
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy import delete, insert, select, update
from starlette.responses import JSONResponse
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import SimulationScope, simulation_state
from test_rule_simulations_body import (
    isolate_stream_from_sentry,  # noqa: F401 -- shared stream isolation fixture
)
from test_simulation_execution import Admission

from app.adapters.simulation_admission import (
    AdmissionResult,
    RedisSimulationAdmission,
    ReleaseResult,
)
from app.api.errors import ApiError, api_error_response
from app.api.rule_simulations import (
    SimulationCodeInput,
    SimulationDiffInput,
    SimulationRules,
    get_simulation_coordinator,
    get_simulation_executor,
)
from app.application.simulation_execution import SimulationExecutionCoordinator
from app.db.base import Base
from app.db.models import Repository, RepositoryAccess
from app.llm.client import BaseLLMClient, LLMCallResult, LLMRequest
from app.llm.errors import (
    SchemaValidationError,
    TransientProviderError,
)
from app.main import create_app
from app.review.simulation import (
    CodeSimulationResult,
    CodeSimulationService,
    DiffSimulationResult,
)

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
        self.failure: BaseException | None = None
        self.deadlines: list[float] = []
        self.entered = asyncio.Event()
        self.finish: asyncio.Event | None = None
        self.ignore_cancellation = False
        self.completed = asyncio.Event()

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, response_model: type[T]
    ) -> LLMCallResult[T]:
        self.requests.append(request)
        self.entered.set()
        if self.finish is not None:
            while not self.finish.is_set():
                try:
                    await self.finish.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancellation:
                        self.completed.set()
                        raise
        self.completed.set()
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


class ObservedRedisAdmission(RedisSimulationAdmission):
    def __init__(self, url: str, state_key: str) -> None:
        super().__init__(
            url,
            state_key=state_key,
            hmac_secret=b"c25a-test-only-secret-material-32bytes!",
        )
        self.url = url
        self.state_key = state_key
        self.acquisitions: list[tuple[UUID, UUID, bytes]] = []
        self.releases: list[str] = []

    async def acquire(
        self, user_id: UUID, repository_id: UUID, idempotency_key: bytes
    ) -> AdmissionResult:
        self.acquisitions.append((user_id, repository_id, idempotency_key))
        return await super().acquire(user_id, repository_id, idempotency_key)

    async def release(self, owner_token: str) -> ReleaseResult:
        self.releases.append(owner_token)
        return await super().release(owner_token)

    async def state(self) -> dict[str, Any]:
        async with Redis.from_url(self.url, decode_responses=True) as client:
            raw = await client.get(self.state_key)
            assert raw is not None
            return json.loads(raw)  # type: ignore[no-any-return]

    async def seed(self, state: dict[str, Any]) -> None:
        async with Redis.from_url(self.url, decode_responses=True) as client:
            await client.set(self.state_key, json.dumps(state))


@pytest.fixture
async def http_admission() -> AsyncIterator[ObservedRedisAdmission]:
    url = os.environ.get("DMC268_TEST_REDIS_URL")
    if not url:
        pytest.skip("requires disposable Redis")
    key = f"dmc268:test:c25a:{uuid4().hex}"
    admission = ObservedRedisAdmission(url, key)
    async with Redis.from_url(url, decode_responses=True) as client:
        await client.set(
            key,
            json.dumps(
                {
                    "schema_version": 3,
                    "hmac_config_id": admission.hmac_config_id,
                    "lease": None,
                    "admissions": [],
                    "consumed_keys": [],
                }
            ),
        )
        try:
            yield admission
        finally:
            await admission.aclose()
            await client.delete(key)


@pytest.fixture
def execution_budget() -> float:
    return 30.0


@pytest.fixture
async def shared_coordinator(
    auth_app: FastAPI,
    http_admission: ObservedRedisAdmission,
    execution_budget: float,
) -> AsyncIterator[SimulationExecutionCoordinator[JSONResponse]]:
    coordinator = SimulationExecutionCoordinator[JSONResponse](
        http_admission, timeout_s=execution_budget, cleanup_timeout_s=0.05
    )
    auth_app.dependency_overrides[get_simulation_coordinator] = lambda: coordinator
    try:
        yield coordinator
    finally:
        assert await coordinator.aclose()
        auth_app.dependency_overrides.pop(get_simulation_coordinator, None)


class HttpExecutor:
    def __init__(self, fake: FakeLLM) -> None:
        self.fake = fake
        self.service = CodeSimulationService(fake)

    async def simulate(
        self,
        code: SimulationCodeInput | SimulationDiffInput,
        rules: SimulationRules,
        *,
        deadline: float,
    ) -> CodeSimulationResult | DiffSimulationResult:
        self.fake.deadlines.append(deadline)
        assert 0 < deadline - asyncio.get_running_loop().time() <= 30.0  # noqa: PLR2004 -- coordinator budget cap
        return await self.service.simulate(code, rules)


@pytest.fixture
async def injected_llm(
    auth_app: FastAPI,
    shared_coordinator: SimulationExecutionCoordinator[JSONResponse],
    http_admission: ObservedRedisAdmission,
) -> AsyncIterator[FakeLLM]:
    fake = FakeLLM()
    service = HttpExecutor(fake)
    auth_app.dependency_overrides[get_simulation_executor] = lambda: service
    try:
        yield fake
    finally:
        auth_app.dependency_overrides.pop(get_simulation_executor, None)


@pytest.mark.parametrize("change", ["access-revoked", "resource-rebound"])
async def test_local_access_changed_during_body_read_prevents_inference(  # noqa: PLR0913, PLR0917 -- real HTTP race and side-effect observations
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    injected_llm: FakeLLM,
    http_admission: ObservedRedisAdmission,
    change: str,
) -> None:
    identity_before = (await browser.get("/api/v1/me")).json()
    cookies_before = dict(browser.cookies)
    github_before = len(github_requests)
    async with postgres.engine.connect() as connection:
        access = dict(
            (
                await connection.execute(
                    select(RepositoryAccess).where(
                        RepositoryAccess.repository_id == simulation_scope.repository
                    )
                )
            )
            .mappings()
            .one()
        )

    async def body() -> AsyncIterator[bytes]:
        assert len(github_requests) == github_before + 1
        async with postgres.engine.begin() as connection:
            if change == "access-revoked":
                await connection.execute(
                    delete(RepositoryAccess).where(RepositoryAccess.id == access["id"])
                )
            else:
                await connection.execute(
                    update(Repository)
                    .where(Repository.id == simulation_scope.repository)
                    .values(external_id=f"{simulation_scope.external_id}-rebound")
                )
        yield json.dumps({"input": CODE, "rules": RULES}).encode()

    try:
        response = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers,
            content=body(),
        )
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.json() == {
            "code": "RESOURCE_NOT_FOUND",
            "message": "Resource was not found.",
            "request_id": response.headers["X-Request-Id"],
            "retryable": False,
            "retry_after": None,
        }
        assert injected_llm.requests == []
        assert http_admission.acquisitions == []
        assert len(github_requests) == github_before + 1
        assert "Set-Cookie" not in response.headers
        assert dict(browser.cookies) == cookies_before
        assert (await browser.get("/api/v1/me")).json() == identity_before
    finally:
        async with postgres.engine.begin() as connection:
            if change == "access-revoked":
                await connection.execute(insert(RepositoryAccess).values(**access))
            else:
                await connection.execute(
                    update(Repository)
                    .where(Repository.id == simulation_scope.repository)
                    .values(external_id=simulation_scope.external_id)
                )


async def test_admin_revoked_during_body_read_prevents_inference(  # noqa: PLR0913, PLR0917 -- HTTP and external side-effect observations
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    provider: dict[str, httpx.Response],
    github_requests: list[httpx.Request],
    injected_llm: FakeLLM,
    http_admission: ObservedRedisAdmission,
) -> None:
    identity_before = (await browser.get("/api/v1/me")).json()
    cookies_before = dict(browser.cookies)
    github_before = len(github_requests)
    path = f"/repositories/{simulation_scope.external_id}"

    async def body() -> AsyncIterator[bytes]:
        assert len(github_requests) == github_before + 1
        repository = provider[path].json()
        repository["permissions"]["admin"] = False
        provider[path] = httpx.Response(200, json=repository)
        yield json.dumps({"input": CODE, "rules": RULES}).encode()

    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        content=body(),
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == {
        "code": "ACCESS_DENIED",
        "message": "Access is denied.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert injected_llm.requests == []
    assert http_admission.acquisitions == []
    assert [request.url.path for request in github_requests[github_before:]] == [
        path
    ] * 2
    assert "Set-Cookie" not in response.headers
    assert dict(browser.cookies) == cookies_before
    assert (await browser.get("/api/v1/me")).json() == identity_before


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
    assert len(injected_llm.deadlines) == 1
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
    ] * 2
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


async def test_unexpected_executor_failure_returns_only_a_safe_http_error(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    api_errors: list[Exception],
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_details = (
        "provider-diagnostics-sentinel offline-credential-sentinel "
        "raw-model-output-sentinel private_source_sentinel private_rules_sentinel"
    )
    injected_llm.failure = RuntimeError(private_details)
    caplog.set_level(logging.DEBUG, logger="app")
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers | {"X-Request-Id": "stage-a-executor-error"},
        json={"input": CODE, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers["X-Request-Id"] == "stage-a-executor-error"
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": "stage-a-executor-error",
        "retryable": False,
        "retry_after": None,
    }
    assert len(api_errors) == 1
    error = api_errors[0]
    assert error.__context__ is None
    assert error.__cause__ is None
    public_details = (
        response.text
        + caplog.text
        + repr(error)
        + repr(vars(error))
        + "".join(traceback.format_exception(error))
    )
    for private in private_details.split():
        assert private not in public_details


async def test_executor_cancellation_propagates_without_an_http_error(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    api_errors: list[Exception],
) -> None:
    cancellation = asyncio.CancelledError()
    injected_llm.failure = cancellation
    with pytest.raises(asyncio.CancelledError) as raised:
        await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers,
            json={"input": CODE, "rules": RULES},
        )
    assert raised.value is cancellation
    assert api_errors == []


async def test_diff_preserves_verified_sides_paths_ranges_refs_and_rejected_count(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    github_requests: list[httpx.Request],
) -> None:
    injected_llm.answer = json.dumps(
        {"findings": [*DIFF_FINDINGS, DIFF_FINDINGS[0] | {"rule_ref": "SEC-99"}]}
    )
    github_before = len(github_requests)
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": DIFF, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"findings": DIFF_FINDINGS, "rejected_count": 1}
    assert len(injected_llm.requests) == 1
    assert [request.url.path for request in github_requests[github_before:]] == [
        f"/repositories/{simulation_scope.external_id}"
    ] * 2


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


@pytest.mark.parametrize("kind", ["code", "diff"])
async def test_http_result_keeps_only_safe_masked_findings(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    input_data = dict(CODE if kind == "code" else DIFF)
    valid = (FINDING if kind == "code" else DIFF_FINDINGS[1]) | {
        "title": "api_key=private-value",
        "explanation": "password=private-value",
    }
    findings = [
        valid,
        valid | {"title": "x" * 194 + " pwd=x"},
        valid | {"rule_ref": "SEC-99"},
    ]
    rejected_count = 2
    if kind == "diff":
        input_data["unified_diff"] += (
            "@@ -9007199254740992 +9007199254740992 @@\n"
            "-old_eval(value)\n+eval(value)\n"
        )
        findings.append(
            valid | {"start_line": 9007199254740992, "end_line": 9007199254740992}
        )
        rejected_count = 3
    injected_llm.answer = json.dumps({"findings": findings})
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": input_data, "rules": RULES},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "findings": [valid | {"title": "api_key=***", "explanation": "password=***"}],
        "rejected_count": rejected_count,
    }
    assert "private-value" not in response.text + caplog.text
    assert "pwd=x" not in response.text + caplog.text


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
        "invalid-session",
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
        "invalid-json",
        "body-user",
        "body-repository",
    ],
)
async def test_rejected_request_never_dispatches_even_with_executor(  # noqa: PLR0913, PLR0917, PLR0912, PLR0915 -- table of ordered HTTP gates
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    provider: dict[str, httpx.Response],
    github_requests: list[httpx.Request],
    injected_llm: FakeLLM,
    gate: str,
    http_admission: ObservedRedisAdmission,
) -> None:
    headers = dict(simulation_scope.headers)
    repository = simulation_scope.repository
    payload = {"input": CODE, "rules": RULES}
    status, code = HTTPStatus.UNPROCESSABLE_CONTENT, "VALIDATION_FAILED"
    if gate in {"session", "invalid-session"}:
        browser.cookies.clear()
        if gate == "invalid-session":
            browser.cookies.set("dmc_session", "invalid")
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
    if gate in {"body-user", "body-repository"}:
        payload["user_id" if gate == "body-user" else "repository_id"] = str(uuid4())  # type: ignore[assignment]
    body = b" " * 262145 if gate == "body-limit" else json.dumps(payload).encode()
    if gate == "invalid-json":
        body = b"not json"
    github_before = len(github_requests)
    response = await browser.post(
        simulation_scope.url(repository), headers=headers, content=body
    )
    assert response.status_code == status
    assert response.json()["code"] == code
    assert injected_llm.requests == []
    assert http_admission.acquisitions == []
    assert (await http_admission.state())["admissions"] == []
    if gate in {"session", "invalid-session", "origin", "csrf", "local-access"}:
        assert len(github_requests) == github_before
    else:
        assert len(github_requests) == github_before + 1


@pytest.mark.parametrize("input_data", [CODE, DIFF], ids=["code", "diff"])
async def test_normal_application_cannot_enable_execution_over_http(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    github_requests: list[httpx.Request],
    input_data: dict[str, str],
) -> None:
    github_before = len(github_requests)
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
    assert [request.url.path for request in github_requests[github_before:]] == [
        f"/repositories/{simulation_scope.external_id}"
    ]


@pytest.mark.parametrize("missing", ["coordinator", "executor", "both"])
async def test_executor_without_coordinator_cannot_bypass_admission(  # noqa: PLR0913, PLR0917
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    auth_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    if missing in {"coordinator", "both"}:
        auth_app.dependency_overrides.pop(get_simulation_coordinator)
    if missing in {"executor", "both"}:
        auth_app.dependency_overrides.pop(get_simulation_executor)
    redis_calls: list[object] = []

    def tripwire(*args: Any, **kwargs: Any) -> Any:
        redis_calls.append((args, kwargs))
        raise AssertionError("disabled runtime contacted Redis")

    with monkeypatch.context() as patch:
        patch.setattr(
            "app.adapters.simulation_admission.create_admission_client", tripwire
        )
        patch.setattr(Redis, "execute_command", tripwire)
        response = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers,
            json={"input": CODE, "rules": RULES},
        )
    assert redis_calls == []
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert injected_llm.requests == []


@pytest.mark.parametrize(
    "status",
    [
        "BUSY",
        "DUPLICATE",
        "RATE_USER",
        "RATE_REPOSITORY",
        "RATE_GLOBAL",
        "INVALID_STATE",
        "UNAVAILABLE",
    ],
)
async def test_admission_denial_is_one_safe_http_503(  # noqa: PLR0913, PLR0917
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    auth_app: FastAPI,
    api_errors: list[Exception],
    status: str,
) -> None:
    admission = Admission(AdmissionResult(status))  # type: ignore[arg-type]
    coordinator = SimulationExecutionCoordinator[JSONResponse](admission)
    auth_app.dependency_overrides[get_simulation_coordinator] = lambda: coordinator
    raw_key = b" Exact-Case-\xff "
    headers = exact_key_headers(simulation_scope, raw_key)
    actor = (await browser.get("/api/v1/me")).json()["user"]["id"]
    try:
        response = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=headers,
            json={"input": CODE, "rules": RULES},
        )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json() == {
            "code": "SERVICE_UNAVAILABLE",
            "message": "Service is unavailable.",
            "request_id": response.headers["X-Request-Id"],
            "retryable": False,
            "retry_after": None,
        }
        assert injected_llm.requests == []
        assert admission.acquisitions == [
            (UUID(actor), simulation_scope.repository, raw_key)
        ]
        assert admission.releases == []
        assert api_errors[0].__context__ is None
        assert api_errors[0].__cause__ is None
    finally:
        assert await coordinator.aclose()
        auth_app.dependency_overrides.pop(get_simulation_coordinator, None)


async def test_result_serialization_cannot_outlive_the_execution_budget(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    auth_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    clock = loop.time

    class BudgetResult(CodeSimulationResult):
        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            value = super().model_dump(**kwargs)
            # Deterministic serialization overrun, without WSL wall-clock sleeps.
            monkeypatch.setattr(loop, "time", lambda: clock() + 31)
            return value

    class BudgetExecutor(HttpExecutor):
        async def simulate(
            self,
            code: SimulationCodeInput | SimulationDiffInput,
            rules: SimulationRules,
            *,
            deadline: float,
        ) -> CodeSimulationResult | DiffSimulationResult:
            result = await super().simulate(code, rules, deadline=deadline)
            return BudgetResult.model_validate(result.model_dump())

    auth_app.dependency_overrides[get_simulation_executor] = lambda: BudgetExecutor(
        injected_llm
    )
    try:
        response = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers,
            json={"input": CODE, "rules": RULES},
        )
    finally:
        monkeypatch.setattr(loop, "time", clock)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert len(injected_llm.requests) == 1


def exact_key_headers(scope: SimulationScope, key: bytes) -> httpx.Headers:
    return httpx.Headers(
        [
            (k.encode("ascii"), v.encode("ascii"))
            for k, v in scope.headers.items()
            if k != "Idempotency-Key"
        ]
        + [(b"Idempotency-Key", key)]
    )


@pytest.mark.parametrize("other_key", [b"Exact-Case-\xff", b" exact-case-\xff "])
async def test_exact_key_suppresses_repeated_and_changed_body_without_replay(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    http_admission: ObservedRedisAdmission,
    other_key: bytes,
) -> None:
    key = b" Exact-Case-\xff "
    actor = UUID((await browser.get("/api/v1/me")).json()["user"]["id"])
    url = simulation_scope.url(simulation_scope.repository)
    response = await browser.post(
        url,
        headers=exact_key_headers(simulation_scope, key),
        json={"input": CODE, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.OK
    assert len(injected_llm.requests) == 1
    initial = await http_admission.state()
    assert initial["lease"] is None
    assert len(initial["admissions"]) == len(initial["consumed_keys"]) == 1
    for source in (CODE, CODE | {"source": "changed_body_sentinel\n"}):
        response = await browser.post(
            url,
            headers=exact_key_headers(simulation_scope, key),
            json={"input": source, "rules": RULES},
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
        assert await http_admission.state() == initial
    response = await browser.post(
        url,
        headers=exact_key_headers(simulation_scope, other_key),
        json={"input": CODE, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.OK
    assert len(injected_llm.requests) == 2  # noqa: PLR2004 -- two distinct admitted keys
    assert http_admission.acquisitions == [
        (actor, simulation_scope.repository, k) for k in (key, key, key, other_key)
    ]
    state = await http_admission.state()
    assert state["lease"] is None
    assert len(state["admissions"]) == len(state["consumed_keys"]) == 2  # noqa: PLR2004
    assert len(http_admission.releases) == 2  # noqa: PLR2004
    assert all(
        record["user_id"] == actor.hex
        and record["repository_id"] == simulation_scope.repository.hex
        for record in state["admissions"]
    )
    for private in (
        "Exact-Case",
        "private_source_sentinel",
        "private_rules_sentinel",
        "changed_body_sentinel",
    ):
        assert private not in json.dumps(state)


@pytest.mark.parametrize("ending", ["exception", "timeout", "cancel"])
@pytest.mark.parametrize("execution_budget", [0.1])
async def test_http_failure_keeps_lease_key_and_allowance(  # noqa: PLR0913, PLR0917
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    http_admission: ObservedRedisAdmission,
    ending: str,
    api_errors: list[Exception],
) -> None:
    if ending == "exception":
        injected_llm.failure = RuntimeError(
            "private_source_sentinel offline-credential-sentinel"
        )
    else:
        injected_llm.finish = asyncio.Event()
    task = asyncio.create_task(
        browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers,
            json={"input": CODE, "rules": RULES},
        )
    )
    await asyncio.wait_for(injected_llm.entered.wait(), 2)
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert api_errors == []
    else:
        response = await task
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
        assert api_errors[0].__context__ is None
        assert api_errors[0].__cause__ is None
    assert len(injected_llm.requests) == 1
    assert http_admission.releases == []
    state = await http_admission.state()
    assert state["lease"] is not None
    assert len(state["admissions"]) == len(state["consumed_keys"]) == 1
    duplicate = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": CODE | {"source": "changed"}, "rules": RULES},
    )
    assert duplicate.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert len(injected_llm.requests) == 1
    assert await http_admission.state() == state
    # Expire only the lease; consumption/rate history remains authoritative.
    state["lease"]["expires_at_ms"] = 1
    await http_admission.seed(state)
    injected_llm.failure = None
    injected_llm.finish = None
    for key in ("second", "third"):
        response = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers | {"Idempotency-Key": key},
            json={"input": CODE, "rules": RULES},
        )
        assert response.status_code == HTTPStatus.OK
    before = await http_admission.state()
    assert len(before["admissions"]) == len(before["consumed_keys"]) == 3  # noqa: PLR2004
    denied = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers | {"Idempotency-Key": "fourth"},
        json={"input": CODE, "rules": RULES},
    )
    assert denied.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert len(injected_llm.requests) == 3  # noqa: PLR2004
    assert await http_admission.state() == before


@pytest.mark.parametrize("execution_budget", [0.1])
async def test_late_success_cannot_become_http_200_or_lose_pending_work(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    http_admission: ObservedRedisAdmission,
    shared_coordinator: SimulationExecutionCoordinator[JSONResponse],
) -> None:
    injected_llm.finish = asyncio.Event()
    injected_llm.ignore_cancellation = True
    try:
        response = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers,
            json={"input": CODE, "rules": RULES},
        )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert not await shared_coordinator.aclose()
        # The same instance retains pending work; another key cannot dispatch.
        again = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers | {"Idempotency-Key": "another"},
            json={"input": CODE, "rules": RULES},
        )
        assert again.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert len(injected_llm.requests) == len(http_admission.acquisitions) == 1
    finally:
        injected_llm.finish.set()
        await asyncio.wait_for(injected_llm.completed.wait(), 2)
        await asyncio.sleep(0)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert http_admission.releases == []
    state = await http_admission.state()
    assert state["lease"] is not None
    assert len(state["admissions"]) == len(state["consumed_keys"]) == 1


async def test_real_redis_unavailable_never_dispatches(  # noqa: PLR0913, PLR0917
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    auth_app: FastAPI,
    caplog: pytest.LogCaptureFixture,
    api_errors: list[Exception],
) -> None:
    admission = RedisSimulationAdmission(
        "redis://127.0.0.1:1/0", hmac_secret=b"c25a-test-only-secret-material-32bytes!"
    )
    coordinator = SimulationExecutionCoordinator[JSONResponse](admission)
    auth_app.dependency_overrides[get_simulation_coordinator] = lambda: coordinator
    try:
        response = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=simulation_scope.headers,
            json={"input": CODE, "rules": RULES},
        )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
        assert injected_llm.requests == []
        assert "127.0.0.1" not in response.text + caplog.text
        assert api_errors[0].__context__ is None
    finally:
        assert await coordinator.aclose()
        await admission.aclose()


@pytest.mark.parametrize(
    "denial", ["BUSY", "RATE_USER", "RATE_REPOSITORY", "RATE_GLOBAL", "INVALID_STATE"]
)
async def test_real_redis_denial_does_not_dispatch_retry_or_change_state(  # noqa: PLR0913, PLR0917
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    injected_llm: FakeLLM,
    http_admission: ObservedRedisAdmission,
    denial: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = await http_admission.state()
    actor = UUID((await browser.get("/api/v1/me")).json()["user"]["id"])
    async with Redis.from_url(http_admission.url) as client:
        seconds, micros = await client.time()
    now = seconds * 1000 + micros // 1000
    if denial == "BUSY":
        state["lease"] = {"owner_token": "a" * 64, "expires_at_ms": now + 90000}
    elif denial == "INVALID_STATE":
        state["private_redis_detail"] = "redis_credentials_sentinel"
    else:
        count = {"RATE_USER": 3, "RATE_REPOSITORY": 10, "RATE_GLOBAL": 20}[denial]
        state["admissions"] = [
            {
                "at_ms": now,
                "user_id": actor.hex if denial == "RATE_USER" else uuid4().hex,
                "repository_id": simulation_scope.repository.hex
                if denial != "RATE_GLOBAL"
                else uuid4().hex,
            }
            for _ in range(count)
        ]
    await http_admission.seed(state)
    response = await browser.post(
        simulation_scope.url(simulation_scope.repository),
        headers=simulation_scope.headers,
        json={"input": CODE, "rules": RULES},
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert len(http_admission.acquisitions) == 1
    assert http_admission.releases == []
    assert injected_llm.requests == []
    assert await http_admission.state() == state
    assert "redis_credentials_sentinel" not in response.text + caplog.text
