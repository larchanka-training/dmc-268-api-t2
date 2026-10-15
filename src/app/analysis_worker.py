"""Process new analyze deliveries using the frozen PostgreSQL composition."""

import argparse
import asyncio
import logging
import math
import signal
import uuid
from pathlib import Path

import httpx
import structlog
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.adapters.redis_queue import RedisQueue
from app.core.config import Logging, get_settings
from app.core.github_runtime import github_connection
from app.core.logging_config import configure_logging
from app.domain.frozen_configuration import FrozenConfigurationError
from app.review.configuration import load_runtime_configuration


def _poll_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be finite and in [0.01, 60]") from None
    if not math.isfinite(interval) or not 0.01 <= interval <= 60:  # noqa: PLR2004
        raise argparse.ArgumentTypeError("must be finite and in [0.01, 60]")
    return interval


async def run(*, once: bool, poll_interval: float) -> None:
    """Own process resources and complete trusted preparation before any receive."""
    runtime = load_runtime_configuration()
    settings = get_settings()
    configure_logging(settings.logging)
    # Transport DEBUG traces include exception repr and server response headers.
    for name in ("httpcore", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
    from app.db.session import async_session_factory, engine  # noqa: PLC0415

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(shutdown_signal, stopping.set)
        from app.adapters.analysis_composition import (  # noqa: PLC0415
            prepare_postgres_analysis_worker,
        )

        async with (
            Redis.from_url(
                settings.redis.url,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry=Retry(NoBackoff(), 0),
            ) as client,
            github_connection(settings) as github,
            httpx.AsyncClient(timeout=120, follow_redirects=False) as http,
        ):
            owner = f"analysis-{uuid.uuid4()}"
            worker = await prepare_postgres_analysis_worker(
                async_session_factory,
                RedisQueue(client),
                runtime=runtime,
                github=github,
                http=http,
                prompt_root=Path(".agents/reviewer"),
                schemas_dir=Path("schemas"),
                owner=owner,
            )
            while not stopping.is_set():
                acknowledged = await worker.run_once()
                structlog.get_logger().info(
                    "analysis_worker_pass", owner=owner, acknowledged=acknowledged
                )
                if once:
                    break
                # False includes deferred/pending work; it never proves an empty PEL.
                if not acknowledged:
                    try:
                        async with asyncio.timeout(poll_interval):
                            await stopping.wait()
                    except TimeoutError:
                        pass
    finally:
        try:
            await engine.dispose()
        finally:
            for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(shutdown_signal)
    structlog.get_logger().info("analysis_worker_stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Process at most one new delivery and exit."
    )
    parser.add_argument(
        "--poll-interval",
        type=_poll_interval,
        default=1.0,
        help="Seconds between idle passes, in [0.01, 60] (1).",
    )
    args = parser.parse_args()
    configure_logging(Logging(format="json", file_enabled=False))
    try:
        asyncio.run(run(once=args.once, poll_interval=args.poll_interval))
    except Exception as error:
        reason = "runtime_failure"
        if isinstance(error, FrozenConfigurationError) and error.reason in {
            "invalid_runtime_manifest",
            "runtime_preflight_failed",
            "invalid_execution_credentials",
        }:
            reason = error.reason
        structlog.get_logger().error(
            "analysis_worker_failed", error_type=type(error).__name__, reason=reason
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
