"""Metrics as structured log events; this deployment has no metrics backend.

Each call emits one ``metric`` event with a name, a value and labels. Labels
are low-cardinality codes: never source, paths, prompts or secrets.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

import structlog


def count(name: str, value: int = 1, **labels: str) -> None:
    structlog.get_logger("metrics").info("metric", metric=name, value=value, **labels)


def record_skipped_files(review_id: UUID, skipped: Iterable[Mapping[str, Any]]) -> None:
    """``files_skipped_total{reason}``: the guard detail, else the wire reason."""
    reasons = Counter(str(item.get("detail") or item["reason"]) for item in skipped)
    for reason, total in sorted(reasons.items()):
        count("files_skipped_total", total, reason=reason, review_id=str(review_id))
"""Small, opt-in process metrics; labels never contain review/provider payloads."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from time import perf_counter

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Metrics, Settings, get_settings

logger = structlog.get_logger(__name__)

HTTP_REQUESTS = Counter(
    "dmc_http_requests_total",
    "Completed HTTP requests",
    ["method", "route", "status_class"],
)
LLM_LATENCY = Histogram(
    "dmc_llm_attempt_seconds",
    "Dispatched provider client attempt latency",
    ["provider", "call_kind", "outcome"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
QUEUE_PENDING = Gauge(
    "dmc_queue_pending", "Pending deliveries, not distinct jobs", ["queue"]
)
QUEUE_LAG = Gauge(
    "dmc_queue_lag", "Not-yet-delivered entries; unknown is NaN", ["queue"]
)
QUEUE_SUCCESS = Gauge(
    "dmc_queue_sample_success", "Queue metadata sample is available", ["queue"]
)


async def sample_queues(client: Redis) -> None:
    """Only XINFO GROUPS; never create/read/claim/ack a delivery."""
    for queue in ("analyze", "publish"):
        pending = lag = float("nan")
        success = 0
        try:
            groups = await client.xinfo_groups(f"dmc268:review-tasks:{queue}")
            group = next(
                (
                    g
                    for g in groups
                    if g.get("name") in ("review-workers", b"review-workers")
                ),
                None,
            )
            if group is not None:
                value = group.get("pending")
                if isinstance(value, int) and value >= 0:
                    pending = float(value)
                    success = 1
                value = group.get("lag")
                if isinstance(value, int) and value >= 0:
                    lag = float(value)
        except Exception:
            # Telemetry failure must invalidate the sample, not stop polling.
            logger.warning("queue_metrics_sample_failed", queue=queue)
        QUEUE_PENDING.labels(queue).set(pending)
        QUEUE_LAG.labels(queue).set(lag)
        QUEUE_SUCCESS.labels(queue).set(success)


@asynccontextmanager
async def queue_metrics(settings: Settings) -> AsyncIterator[None]:
    """API-owned read-only sampling; other processes expose only their own metrics."""
    if not settings.metrics.enabled:
        yield
        return
    async with Redis.from_url(
        settings.redis.url,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    ) as client:
        await sample_queues(client)

        async def poll() -> None:
            while True:
                await asyncio.sleep(15)
                await sample_queues(client)

        task = asyncio.create_task(poll())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@contextmanager
def llm_attempt(provider: str, call_kind: str) -> Iterator[None]:
    """Measure dispatch, never budget reservation or provider-reported strings."""
    if not get_settings().metrics.enabled:
        yield
        return
    provider = provider if provider in {"eurorouter", "ollama"} else "other"
    call_kind = (
        call_kind
        if call_kind in {"initial", "repair", "overflow_rebuild", "reflection"}
        else "other"
    )
    started = perf_counter()
    outcome = "error"
    try:
        yield
        outcome = "success"
    finally:
        LLM_LATENCY.labels(provider, call_kind, outcome).observe(
            perf_counter() - started
        )


@contextmanager
def metrics_server(settings: Metrics) -> Iterator[None]:
    """Own the listener for one process lifetime, never at import time."""
    if not settings.enabled:
        yield
        return
    server, thread = start_http_server(settings.port, addr=settings.host)
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class MetricsMiddleware:
    """Observe status without buffering HTTP/SSE bodies or changing exceptions."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope["method"]
            if method not in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
            }:
                method = "OTHER"
            HTTP_REQUESTS.labels(method, route, f"{status // 100}xx").inc()
