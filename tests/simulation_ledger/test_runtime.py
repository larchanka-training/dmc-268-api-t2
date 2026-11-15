"""Required ledger composition at the actual runtime/dependency boundary."""

import pytest
from starlette.requests import Request
from test_simulation_runtime import (  # noqa: F401
    enable_configuration,
    isolated_runtime_environment,
)

from app.api.rule_simulations import get_simulation_coordinator, get_simulation_executor
from app.main import create_app


async def test_enabled_runtime_requires_its_owned_postgres_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_configuration(monkeypatch)
    app = create_app()
    async with app.router.lifespan_context(app):
        runtime = app.state.simulation_runtime
        assert runtime.ledger is not None
        assert runtime.coordinator.ledger is runtime.ledger
        request = Request({"type": "http", "app": app})
        assert await get_simulation_executor(request) is runtime.executor
        ledger = runtime.ledger
        runtime.ledger = None
        try:
            assert await get_simulation_executor(request) is None
            assert await get_simulation_coordinator(request) is None
        finally:
            runtime.ledger = ledger
