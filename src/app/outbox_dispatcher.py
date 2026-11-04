"""Publish PostgreSQL outbox commands to Redis Streams."""

import argparse
import asyncio
import math
import signal
from time import monotonic

import structlog
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.core.config import Logging, get_settings
from app.core.logging_config import configure_logging
from app.core.sentry import configure_sentry


def _batch_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if size <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return size


def _poll_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "must be finite and positive (at least 0.01 seconds)"
        ) from None
    if not math.isfinite(interval) or interval < 0.01:  # noqa: PLR2004
        raise argparse.ArgumentTypeError(
            "must be finite and positive (at least 0.01 seconds)"
        )
    return interval


async def _run(*, once: bool, batch_size: int, poll_interval: float) -> None:
    settings = get_settings()
    configure_logging(settings.logging)
    # Delay settings-dependent engine creation until errors can be sanitized.
    from app.db.session import async_session_factory, engine  # noqa: PLC0415

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(shutdown_signal, stopping.set)
    try:
        try:
            async with Redis.from_url(
                settings.redis.url,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry=Retry(NoBackoff(), 0),
            ) as client:
                dispatcher = PostgresOutboxDispatcher(
                    async_session_factory, RedisQueue(client)
                )
                next_recovery = monotonic()
                while not stopping.is_set():
                    await client.ping()
                    if stopping.is_set():
                        break
                    if monotonic() >= next_recovery:
                        recovered = await dispatcher.recover_batch()
                        next_recovery = monotonic() + 30
                        structlog.get_logger().info(
                            "outbox_recovery", confirmed=recovered
                        )
                    if stopping.is_set():
                        break
                    confirmed = await dispatcher.dispatch_batch(batch_size=batch_size)
                    structlog.get_logger().info("outbox_batch", confirmed=confirmed)
                    if once:
                        break
                    try:
                        delay = min(poll_interval, max(0, next_recovery - monotonic()))
                        async with asyncio.timeout(delay):
                            await stopping.wait()
                    except TimeoutError:
                        pass
        finally:
            await engine.dispose()
        structlog.get_logger().info("outbox_stopped")
    finally:
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(shutdown_signal)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Process one bounded batch and exit."
    )
    parser.add_argument(
        "--batch-size",
        type=_batch_size,
        default=100,
        help="Maximum events per batch (100).",
    )
    parser.add_argument(
        "--poll-interval",
        type=_poll_interval,
        default=5.0,
        help="Seconds between batches, including empty batches (5; minimum 0.01).",
    )
    args = parser.parse_args()
    configure_logging(Logging(format="json", file_enabled=False))
    try:
        configure_sentry(get_settings())
        asyncio.run(
            _run(
                once=args.once,
                batch_size=args.batch_size,
                poll_interval=args.poll_interval,
            )
        )
    except Exception as error:
        structlog.get_logger().error("outbox_failed", error_type=type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
