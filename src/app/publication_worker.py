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
    from app.core.config import Settings


async def run_once(
    sessions: async_sessionmaker[AsyncSession],
    queue: RedisQueue,
    *,
    settings: Settings,
    owner: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    from app.adapters.publication_dispatcher import (  # noqa: PLC0415
        PostgresPublicationDispatcher,
    )
    from app.adapters.review_worker import PostgresReviewWorker  # noqa: PLC0415
    from app.application.ports import DurableTaskOutcome  # noqa: PLC0415
    from app.core.publication_runtime import make_publication_handler  # noqa: PLC0415

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
            while not stopping.is_set():
                try:
                    acknowledged = await run_once(
                        async_session_factory,
                        RedisQueue(client),
                        settings=settings,
                        owner=owner,
                        transport=transport,
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
    parser.add_argument(
        "--once", action="store_true", help="Dispatch and process one bounded pass."
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
        configure_sentry(get_settings())
        asyncio.run(run(once=args.once, poll_interval=args.poll_interval))
    except Exception as error:
        structlog.get_logger().error(
            "publication_worker_failed", error_type=type(error).__name__
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
