"""Dispatch and process persisted GitHub publication commands."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.adapters.redis_queue import RedisQueue
    from app.core.circuit_breaker import CircuitBreaker
    from app.core.config import Settings


async def run_once(  # noqa: PLR0913 -- explicit runtime inputs
    sessions: async_sessionmaker[AsyncSession],
    queue: RedisQueue,
    *,
    settings: Settings,
    owner: str,
    transport: httpx.AsyncBaseTransport | None = None,
    breaker: CircuitBreaker | None = None,
) -> bool:
    from app.adapters.publication_dispatcher import (  # noqa: PLC0415
        PostgresPublicationDispatcher,
    )
    from app.adapters.review_worker import PostgresReviewWorker  # noqa: PLC0415
    from app.application.ports import DurableTaskOutcome  # noqa: PLC0415
    from app.application.use_cases.sync_commit_statuses import (  # noqa: PLC0415
        sync_commit_statuses,
    )
    from app.core.publication_runtime import (  # noqa: PLC0415
        github_publisher_connection,
        make_publication_handler,
    )
    from app.db.commit_statuses import PostgresCommitStatusStore  # noqa: PLC0415

    # Statuses use the same write identity; their own failures back off per review.
    async with github_publisher_connection(
        settings, transport=transport, breaker=breaker
    ) as github:
        await sync_commit_statuses(
            PostgresCommitStatusStore(sessions),
            github,
            context=settings.github.status_context,
            report_base_url=settings.frontend.base_url,
        )
    # No breaker here: a fast-failed pre-POST read would park the review UNKNOWN.
    async with make_publication_handler(
        settings, sessions, DurableTaskOutcome.COMPLETED, transport=transport
    ) as handler:
        await PostgresPublicationDispatcher(sessions, queue).dispatch_batch()
        worker = PostgresReviewWorker(
            sessions, queue, handler, owner=owner, task_kind="publish"
        )
        return await worker.run_once()


async def run(
    *,
    once: bool,
    poll_interval: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    import structlog  # noqa: PLC0415
    from redis.asyncio import Redis  # noqa: PLC0415
    from redis.asyncio.retry import Retry  # noqa: PLC0415
    from redis.backoff import NoBackoff  # noqa: PLC0415

    from app.adapters.publication_handler import (  # noqa: PLC0415
        PublicationRecoveryRequiredError,
    )
    from app.adapters.redis_queue import RedisQueue  # noqa: PLC0415
    from app.application.ports import LeaseLostError  # noqa: PLC0415
    from app.application.publication_ports import (  # noqa: PLC0415
        PublicationAuthorityError,
    )
    from app.core.circuit_breaker import CircuitBreaker  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.logging_config import configure_logging  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings.logging)
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    from app.db.session import async_session_factory, engine  # noqa: PLC0415
    from app.db.worker_tasks import QueueIdentityError  # noqa: PLC0415

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(shutdown_signal, stopping.set)
    try:
        async with Redis.from_url(
            settings.redis.url,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry=Retry(NoBackoff(), 0),
        ) as client:
            owner = f"publication-{uuid.uuid4()}"
            # One breaker per process: it must outlive each pass's HTTP client.
            breaker = CircuitBreaker()
            while not stopping.is_set():
                try:
                    acknowledged = await run_once(
                        async_session_factory,
                        RedisQueue(client),
                        settings=settings,
                        owner=owner,
                        transport=transport,
                        breaker=breaker,
                    )
                except (
                    PublicationRecoveryRequiredError,
                    PublicationAuthorityError,
                    LeaseLostError,
                    QueueIdentityError,
                ) as error:
                    if once:
                        raise
                    acknowledged = False
                    structlog.get_logger().warning(
                        "publication_worker_delivery_deferred",
                        owner=owner,
                        error_type=type(error).__name__,
                    )
                structlog.get_logger().info(
                    "publication_worker_pass", owner=owner, acknowledged=acknowledged
                )
                if once:
                    break
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
    structlog.get_logger().info("publication_worker_stopped")


def _poll_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be finite and in [0.01, 60]") from None
    if not math.isfinite(interval) or not 0.01 <= interval <= 60:  # noqa: PLR2004
        raise argparse.ArgumentTypeError("must be finite and in [0.01, 60]")
    return interval


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true", help="Dispatch and process one bounded pass."
    )
    mode.add_argument(
        "--check-config",
        action="store_true",
        help="Validate local publication credentials without external side effects.",
    )
    parser.add_argument(
        "--poll-interval",
        type=_poll_interval,
        default=1.0,
        help="Seconds between idle passes, in [0.01, 60] (1).",
    )
    args = parser.parse_args()
    import structlog  # noqa: PLC0415 -- keep argument parsing independent

    from app.core.config import Logging, get_settings  # noqa: PLC0415
    from app.core.logging_config import configure_logging  # noqa: PLC0415
    from app.core.sentry import configure_sentry  # noqa: PLC0415

    configure_logging(Logging(format="json", file_enabled=False))
    try:
        if args.check_config:
            from app.core.publication_runtime import (  # noqa: PLC0415 -- offline mode
                validate_publication_settings,
            )

            validate_publication_settings(get_settings())
            print("Publication configuration is valid.")
            return
        from app.core.metrics import (  # noqa: PLC0415 -- keep offline mode offline
            metrics_server,
        )

        configure_sentry(get_settings())
        with metrics_server(get_settings().metrics):
            asyncio.run(run(once=args.once, poll_interval=args.poll_interval))
    except Exception as error:
        structlog.get_logger().error(
            "publication_worker_failed", error_type=type(error).__name__
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
