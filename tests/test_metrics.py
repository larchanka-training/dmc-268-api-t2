"""Optional internal metrics through real HTTP and process lifecycles."""

import socket
import sys
from http import HTTPStatus
from types import ModuleType
from typing import cast
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app import analysis_worker, outbox_dispatcher, publication_worker, webhook_worker
from app.core.config import get_settings
from app.core.metrics import sample_queues
from app.main import create_app


@pytest.fixture(autouse=True)
def no_shared_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyRedis:
        async def __aenter__(self) -> EmptyRedis:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def xinfo_groups(self, name: str) -> list[dict[str, object]]:
            return []

    monkeypatch.setattr(
        "redis.asyncio.Redis.from_url", lambda *args, **kwargs: EmptyRedis()
    )


def worker_listener_case(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, run_name: str
) -> None:
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    monkeypatch.setenv("METRICS__ENABLED", "true")
    monkeypatch.setenv("METRICS__HOST", "127.0.0.1")
    monkeypatch.setenv("METRICS__PORT", str(port))
    monkeypatch.setenv("SENTRY__DSN", "")
    monkeypatch.setattr(sys, "argv", [module.__name__, "--once"])
    get_settings.cache_clear()

    async def work(**kwargs: object) -> None:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            assert response.status == HTTPStatus.OK

    monkeypatch.setattr(module, run_name, work)
    try:
        module.main()
        with pytest.raises(URLError):
            urlopen(f"http://127.0.0.1:{port}/", timeout=1)
    finally:
        get_settings.cache_clear()


def test_analysis_process_owns_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_listener_case(monkeypatch, analysis_worker, "run")


def test_publication_process_owns_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_listener_case(monkeypatch, publication_worker, "run")


def test_webhook_process_owns_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_listener_case(monkeypatch, webhook_worker, "_run")


def test_dispatcher_process_owns_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_listener_case(monkeypatch, outbox_dispatcher, "_run")


def test_opt_in_metrics_count_safe_route_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    monkeypatch.setenv("METRICS__ENABLED", "true")
    monkeypatch.setenv("METRICS__HOST", "127.0.0.1")
    monkeypatch.setenv("METRICS__PORT", str(port))
    monkeypatch.setenv("SENTRY__DSN", "")
    get_settings.cache_clear()
    app = create_app()

    @app.get("/metrics-probe/{item_id}")
    async def read_item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    try:
        with TestClient(app) as client:
            for item in ("private-one", "private-two"):
                assert (
                    client.get(f"/metrics-probe/{item}?token=secret-query").status_code
                    == HTTPStatus.OK
                )
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    metrics = response.read().decode()
            except URLError:
                metrics = ""
            assert (
                'dmc_http_requests_total{method="GET",route="/metrics-probe/{item_id}",'
                'status_class="2xx"} 2.0' in metrics
            )
            assert "private-one" not in metrics
            assert "secret-query" not in metrics
    finally:
        get_settings.cache_clear()


def test_queue_samples_are_read_only_and_outage_is_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    class RedisBoundary:
        async def __aenter__(self) -> RedisBoundary:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def xinfo_groups(self, name: str) -> list[dict[str, object]]:
            commands.append(name)
            if name.endswith(":publish"):
                raise RedisConnectionError("private-redis-credential")
            return [{"name": b"review-workers", "pending": 1, "lag": 3}]

    monkeypatch.setattr(
        "redis.asyncio.Redis.from_url", lambda *args, **kwargs: RedisBoundary()
    )
    monkeypatch.setenv("METRICS__ENABLED", "true")
    monkeypatch.setenv("METRICS__PORT", "19126")
    monkeypatch.setenv("SENTRY__DSN", "")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()):
            metrics = generate_latest().decode()
            assert 'dmc_queue_pending{queue="analyze"} 1.0' in metrics
            assert 'dmc_queue_lag{queue="analyze"} 3.0' in metrics
            assert 'dmc_queue_sample_success{queue="analyze"} 1.0' in metrics
            assert 'dmc_queue_sample_success{queue="publish"} 0.0' in metrics
            assert 'dmc_queue_pending{queue="publish"} NaN' in metrics
            assert "private-redis-credential" not in metrics
        assert commands == [
            "dmc268:review-tasks:analyze",
            "dmc268:review-tasks:publish",
        ]
    finally:
        get_settings.cache_clear()


def test_http_observer_preserves_unhandled_500_and_bounds_unknown_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASGI compatibility: preserve errors and never label an unmatched raw path."""
    monkeypatch.setenv("METRICS__ENABLED", "true")
    monkeypatch.setenv("SENTRY__DSN", "")
    get_settings.cache_clear()
    app = create_app()

    @app.get("/metrics-broken")
    async def broken() -> None:
        raise RuntimeError("synthetic failure")

    try:
        client = TestClient(app, raise_server_exceptions=False)
        assert (
            client.get("/metrics-broken").status_code
            == HTTPStatus.INTERNAL_SERVER_ERROR
        )
        assert (
            client.get("/unknown-private-path-one").status_code == HTTPStatus.NOT_FOUND
        )
        assert (
            client.get("/unknown-private-path-two").status_code == HTTPStatus.NOT_FOUND
        )
        metrics = generate_latest().decode()
        assert (
            'dmc_http_requests_total{method="GET",route="/metrics-broken",'
            'status_class="5xx"} 1.0' in metrics
        )
        assert "unknown-private-path" not in metrics
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unexpected_queue_sample_failure_is_visible_and_can_recover(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FlakyRedis:
        failing = False

        async def xinfo_groups(self, name: str) -> list[dict[str, object]]:
            if self.failing:
                raise ValueError("private-queue-diagnostic")
            return [{"name": "review-workers", "pending": 2, "lag": 4}]

    client = FlakyRedis()
    await sample_queues(cast(Redis, client))
    assert 'dmc_queue_sample_success{queue="analyze"} 1.0' in generate_latest().decode()

    client.failing = True
    caplog.set_level("WARNING")
    await sample_queues(cast(Redis, client))
    metrics = generate_latest().decode()
    assert 'dmc_queue_sample_success{queue="analyze"} 0.0' in metrics
    assert 'dmc_queue_pending{queue="analyze"} NaN' in metrics
    assert 'dmc_queue_lag{queue="publish"} NaN' in metrics
    assert "queue_metrics_sample_failed" in caplog.text
    assert "private-queue-diagnostic" not in caplog.text

    client.failing = False
    await sample_queues(cast(Redis, client))
    assert 'dmc_queue_sample_success{queue="analyze"} 1.0' in generate_latest().decode()
