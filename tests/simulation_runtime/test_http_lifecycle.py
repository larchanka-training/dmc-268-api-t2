"""Real auth/CSRF/admin, PostgreSQL, Redis and offline provider through lifespan."""

# ruff: noqa: PLR0913, PLR0917, PLR2004 -- explicit integration fixtures and AC literals

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from test_rule_simulations_access import SimulationScope
from test_simulation_runtime import MANIFEST

from app.adapters.simulation_admission import STATE_KEY
from app.core.config import get_settings
from app.main import create_app

from .conftest import ProviderIO, protocol_lifespan, provision

CODE = {"kind": "code", "path": "example.py", "source": "eval(value)\n"}
RULES = {
    "format": "rules-md/1",
    "markdown": "# Security\n## SEC-1: No eval\nAvoid eval.",
}


async def simulate(
    browser: httpx.AsyncClient,
    scope: SimulationScope,
    key: str = "runtime-one",
    code: dict[str, Any] | None = None,
) -> httpx.Response:
    return await browser.post(
        scope.url(scope.repository),
        headers=scope.headers | {"Idempotency-Key": key},
        json={"input": code or CODE, "rules": RULES},
    )


async def test_pending_shutdown_retains_resources_then_observes_late_exception(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_io.finish = asyncio.Event()
    provider_io.ignore_cancellation = True
    provider_io.failure = RuntimeError("synthetic-late-private-provider-error")
    redis_closes: list[Redis] = []
    original_close = Redis.aclose

    async def observe_close(client: Redis, *args: Any, **kwargs: Any) -> None:
        redis_closes.append(client)
        await original_close(client, *args, **kwargs)

    monkeypatch.setattr(Redis, "aclose", observe_close)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))
    lifespan = protocol_lifespan(auth_app)
    await lifespan.__aenter__()
    runtime = auth_app.state.simulation_runtime
    await provision(admission_state, auth_app)
    request = asyncio.create_task(simulate(browser, simulation_scope))
    try:
        await asyncio.wait_for(provider_io.entered.wait(), 2)
        started = loop.time()
        await asyncio.wait_for(lifespan.__aexit__(None, None, None), 3)
        assert loop.time() - started < 3
        assert runtime.http.is_closed is False
        assert redis_closes == []
        assert not runtime.closed
        assert (
            await simulate(browser, simulation_scope, "after-shutdown")
        ).status_code == 503
        assert len(provider_io.requests) == 1
        provider_io.finish.set()
        # A repeated explicit call is not required for eventual cleanup.
        async with asyncio.timeout(3):
            while not runtime.http.is_closed:
                await asyncio.sleep(0.01)
        await asyncio.gather(request, return_exceptions=True)
        await asyncio.wait_for(asyncio.shield(runtime.cleanup_task), 3)
        assert runtime.closed
        assert len(redis_closes) == 1
        state = await read_state(admission_state)
        assert state["lease"] is not None
        assert len(state["consumed_keys"]) == 1
        assert unhandled == []
    finally:
        provider_io.finish.set()
        await asyncio.gather(request, return_exceptions=True)
        loop.set_exception_handler(previous)


async def test_reentered_lifespan_cannot_replace_pending_resource_owner(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
) -> None:
    provider_io.finish = asyncio.Event()
    provider_io.ignore_cancellation = True
    lifespan = auth_app.router.lifespan_context(auth_app)
    await lifespan.__aenter__()
    owner = auth_app.state.simulation_runtime
    await provision(admission_state, auth_app)
    request = asyncio.create_task(simulate(browser, simulation_scope))
    try:
        await asyncio.wait_for(provider_io.entered.wait(), 2)
        await asyncio.wait_for(lifespan.__aexit__(None, None, None), 3)
        async with auth_app.router.lifespan_context(auth_app):
            assert auth_app.state.simulation_runtime is owner
            assert (
                await simulate(browser, simulation_scope, "replacement")
            ).status_code == 503
            assert len(provider_io.requests) == 1
    finally:
        provider_io.finish.set()
        await asyncio.gather(request, return_exceptions=True)
        await asyncio.wait_for(asyncio.shield(owner.cleanup_task), 3)


DIFF = {
    "kind": "diff",
    "unified_diff": (
        "diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n"
        "@@ -1 +1 @@\n-safe(value)\n+eval(value)\n"
    ),
}
FINDING = {
    "path": "example.py",
    "start_line": 1,
    "end_line": 1,
    "rule_ref": "SEC-1",
    "title": "Unsafe evaluation",
    "explanation": "The rule prohibits eval.",
    "evidence": "eval(value)",
}


def assert_unavailable(response: httpx.Response) -> None:
    assert response.status_code == 503
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }


@pytest.mark.parametrize("code", [CODE, DIFF], ids=["code", "diff"])
async def test_verified_code_and_diff_reuse_runtime_and_consume_keys(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
    code: dict[str, Any],
) -> None:
    finding = FINDING | ({"side": "new"} if code["kind"] == "diff" else {})
    provider_io.output = {"findings": [finding]}
    async with auth_app.router.lifespan_context(auth_app):
        owner = auth_app.state.simulation_runtime
        pair = (owner.executor, owner.coordinator, owner.http, owner.admission)
        await provision(admission_state, auth_app)
        response = await simulate(browser, simulation_scope, code=code)
        assert response.status_code == 200
        assert response.json() == {"findings": [finding], "rejected_count": 0}
        assert len(provider_io.requests) == 1
        body = json.loads(provider_io.requests[0].content)
        assert body["model"] == "mistral-small-4"
        assert body["max_tokens"] == 2000
        assert all(
            0 < value <= 30
            for value in provider_io.requests[0].extensions["timeout"].values()
        )
        state = await admission_state.get(STATE_KEY)
        assert_unavailable(await simulate(browser, simulation_scope, code=code))
        assert len(provider_io.requests) == 1
        assert await admission_state.get(STATE_KEY) == state
        assert (
            await simulate(browser, simulation_scope, "runtime-two", code)
        ).status_code == 200
        assert len(provider_io.requests) == 2
        assert pair == (owner.executor, owner.coordinator, owner.http, owner.admission)
        persisted = await read_state(admission_state)
        assert persisted["lease"] is None
        assert len(persisted["consumed_keys"]) == 2
        assert "runtime-one" not in json.dumps(persisted)
        assert "eval(value)" not in json.dumps(persisted)
    assert owner.closed
    assert owner.http.is_closed


@pytest.mark.parametrize(
    "defect",
    [
        "disabled",
        "missing-manifest",
        "missing-path",
        "credentials",
        "missing-secret",
        "invalid-secret",
        "model",
        "endpoint",
        "fallback",
        "provider",
    ],
)
async def test_disabled_and_misconfigured_runtime_keep_http_available_without_clients(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    provider_io: ProviderIO,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:

    if defect == "disabled":
        monkeypatch.setenv("SIMULATION__ENABLED", "false")
    elif defect == "missing-manifest":
        monkeypatch.setenv(
            "SIMULATION__MANIFEST_PATH", "/nonexistent/test-only-manifest.json"
        )
    elif defect == "missing-path":
        monkeypatch.setenv(
            "ANALYSIS__CONFIG_PATH", os.environ["SIMULATION__MANIFEST_PATH"]
        )
        monkeypatch.delenv("SIMULATION__MANIFEST_PATH")
    elif defect == "credentials":
        monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "synthetic invalid credential")
    elif defect == "missing-secret":
        monkeypatch.delenv("SIMULATION__HMAC_SECRET_B64")
    elif defect == "invalid-secret":
        monkeypatch.setenv("SIMULATION__HMAC_SECRET_B64", "invalid!")
    elif defect == "model":
        monkeypatch.setenv("LLM__EUROROUTER__MODEL", "unapproved-model")
    elif defect == "endpoint":
        monkeypatch.setenv("LLM__EUROROUTER__BASE_URL", "https://unapproved.invalid")
    elif defect == "fallback":
        monkeypatch.setenv("LLM__FALLBACK_ENABLED", "true")
    else:
        monkeypatch.setenv("LLM__PRIMARY_PROVIDER", "ollama")
    get_settings.cache_clear()
    async with auth_app.router.lifespan_context(auth_app):
        runtime = auth_app.state.simulation_runtime
        assert (
            runtime.executor
            is runtime.coordinator
            is runtime.http
            is runtime.admission
            is None
        )
        assert_unavailable(await simulate(browser, simulation_scope))
        assert (await browser.get("/healthcheck")).status_code == 200
        assert (await browser.get("/")).status_code == 200
        assert provider_io.requests == []


async def test_missing_redis_state_is_never_provisioned_and_never_dispatches(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
) -> None:
    async with auth_app.router.lifespan_context(auth_app):
        assert_unavailable(await simulate(browser, simulation_scope))
        assert await admission_state.get(STATE_KEY) is None
        assert provider_io.requests == []


@pytest.mark.parametrize("status", [429, 503])
async def test_provider_http_failure_is_one_call_without_retry_or_secret_diagnostics(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider_io.status = status
    provider_io.output = {"private": "synthetic-private-response"}
    async with auth_app.router.lifespan_context(auth_app):
        await provision(admission_state, auth_app)
        response = await simulate(browser, simulation_scope)
        assert_unavailable(response)
        assert len(provider_io.requests) == 1
        state = await read_state(admission_state)
        assert state["lease"] is not None
        diagnostics = response.text + caplog.text
        for secret in (
            "synthetic-first-key",
            "synthetic-second-key",
            "synthetic-private-response",
            "runtime-one",
        ):
            assert secret not in diagnostics


async def test_provider_exception_is_canonical_503_without_diagnostics(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider_io.failure = RuntimeError("synthetic-private-provider-error")
    async with auth_app.router.lifespan_context(auth_app):
        await provision(admission_state, auth_app)
        response = await simulate(browser, simulation_scope)
        assert_unavailable(response)
        assert len(provider_io.requests) == 1
        assert "synthetic-private-provider-error" not in response.text + caplog.text
        assert "synthetic-first-key" not in response.text + caplog.text


async def test_timeout_uses_manifest_budget_and_preserves_unknown_lease(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    manifest = json.loads(MANIFEST.read_text())
    manifest["admission"]["policy"]["limits"]["request_timeout_ms"] = 50
    path = tmp_path / "trusted-test-manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("SIMULATION__MANIFEST_PATH", str(path))
    get_settings.cache_clear()
    provider_io.finish = asyncio.Event()
    async with auth_app.router.lifespan_context(auth_app):
        await provision(admission_state, auth_app)
        assert_unavailable(await simulate(browser, simulation_scope))
        assert len(provider_io.requests) == 1
        assert all(
            value <= 0.05
            for value in provider_io.requests[0].extensions["timeout"].values()
        )
        state = await read_state(admission_state)
        assert state["lease"] is not None
        assert len(state["consumed_keys"]) == 1


async def test_request_cancellation_propagates_and_keeps_lease(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
) -> None:
    provider_io.finish = asyncio.Event()
    async with auth_app.router.lifespan_context(auth_app):
        await provision(admission_state, auth_app)
        request = asyncio.create_task(simulate(browser, simulation_scope))
        await asyncio.wait_for(provider_io.entered.wait(), 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert len(provider_io.requests) == 1
        assert (await read_state(admission_state))["lease"] is not None


@pytest.mark.parametrize("guard", ["authentication", "csrf", "admin"])
async def test_authorization_precedes_any_admission_or_provider_call(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
    provider: dict[str, httpx.Response],
    guard: str,
) -> None:
    async with auth_app.router.lifespan_context(auth_app):
        await provision(admission_state, auth_app)
        before = await admission_state.get(STATE_KEY)
        headers = dict(simulation_scope.headers)
        if guard == "authentication":
            browser.cookies.clear()
            expected = 401
        elif guard == "csrf":
            headers.pop("X-CSRF-Token")
            expected = 403
        else:
            url = f"/repositories/{simulation_scope.external_id}"
            repository = provider[url].json()
            repository["permissions"]["admin"] = False
            provider[url] = httpx.Response(200, json=repository)
            expected = 403
        response = await browser.post(
            simulation_scope.url(simulation_scope.repository),
            headers=headers,
            json={"input": CODE, "rules": RULES},
        )
        assert response.status_code == expected
        assert provider_io.requests == []
        assert await admission_state.get(STATE_KEY) == before


async def test_concurrent_requests_share_redis_concurrency_across_applications(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
) -> None:

    other = create_app()
    other.dependency_overrides.update(auth_app.dependency_overrides)
    provider_io.finish = asyncio.Event()
    async with (
        auth_app.router.lifespan_context(auth_app),
        other.router.lifespan_context(other),
    ):
        owner = auth_app.state.simulation_runtime
        second = other.state.simulation_runtime
        assert owner is not second
        assert owner.admission is not second.admission
        await provision(admission_state, auth_app)
        first = asyncio.create_task(simulate(browser, simulation_scope))
        try:
            await asyncio.wait_for(provider_io.entered.wait(), 2)
            assert_unavailable(
                await simulate(browser, simulation_scope, "local-concurrent")
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(other),
                base_url="https://test",
                cookies=browser.cookies,
            ) as second_browser:
                assert_unavailable(
                    await simulate(
                        second_browser, simulation_scope, "other-app-concurrent"
                    )
                )
            assert len(provider_io.requests) == 1
            assert len((await read_state(admission_state))["admissions"]) == 1
        finally:
            provider_io.finish.set()
            assert (await first).status_code == 200


async def test_shutdown_during_confirmed_success_releases_before_closing_resources(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
) -> None:
    provider_io.finish = asyncio.Event()
    provider_io.ignore_cancellation = True
    lifespan = auth_app.router.lifespan_context(auth_app)
    await lifespan.__aenter__()
    runtime = auth_app.state.simulation_runtime
    await provision(admission_state, auth_app)
    request = asyncio.create_task(simulate(browser, simulation_scope))
    await asyncio.wait_for(provider_io.entered.wait(), 2)
    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    try:
        while runtime.accepting:
            await asyncio.sleep(0)
        assert not runtime.http.is_closed
        provider_io.finish.set()
        assert (await request).status_code == 200
        await asyncio.wait_for(shutdown, 3)
        assert runtime.closed
        assert runtime.http.is_closed
        assert (await read_state(admission_state))["lease"] is None
        assert len(provider_io.requests) == 1
    finally:
        provider_io.finish.set()
        await asyncio.gather(request, shutdown, return_exceptions=True)


async def read_state(client: Redis) -> dict[str, Any]:
    raw = await client.get(STATE_KEY)
    assert isinstance(raw, str)
    value: dict[str, Any] = json.loads(raw)
    return value


@pytest.mark.parametrize("size", [32, 1024])
async def test_configured_secret_is_stable_across_restart_with_existing_state(
    auth_app: FastAPI,
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    admission_state: Redis,
    provider_io: ProviderIO,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
) -> None:
    monkeypatch.setenv(
        "SIMULATION__HMAC_SECRET_B64",
        base64.b64encode(b"stable-secret".ljust(size, b"s")).decode(),
    )
    get_settings.cache_clear()
    async with auth_app.router.lifespan_context(auth_app):
        first = auth_app.state.simulation_runtime
        identity = first.admission.hmac_config_id
        await provision(admission_state, auth_app)
        assert (await simulate(browser, simulation_scope)).status_code == 200
    before = await admission_state.get(STATE_KEY)
    async with auth_app.router.lifespan_context(auth_app):
        second = auth_app.state.simulation_runtime
        assert second is not first
        assert second.admission.hmac_config_id == identity
        assert await admission_state.get(STATE_KEY) == before
        assert_unavailable(await simulate(browser, simulation_scope))
        assert (
            await simulate(browser, simulation_scope, "after-restart")
        ).status_code == 200
        assert len(provider_io.requests) == 2
