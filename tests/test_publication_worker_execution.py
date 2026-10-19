"""Actual publication runtime composition against persisted analysis and Redis."""

import os
import subprocess
import sys
import uuid
from pathlib import Path
from textwrap import dedent
from typing import Any

import httpx
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_github_publisher_postgres import FakeGithub
from test_publication_dispatcher import publication_command

from app.adapters.redis_queue import RedisQueue
from app.core.config import Github, Settings
from app.db.publications import PostgresPublicationStore
from app.publication_worker import run_once

pytest_plugins = (
    "test_publications_postgres",
    "test_redis_queue",
    "test_publication_worker_cli",
)


async def test_runtime_publishes_persisted_command_and_acks_without_llm_configuration(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
    redis_client: Redis,
    stream_prefix: str,
) -> None:
    command = await publication_command(publication_engine, payload_data)
    sessions = async_sessionmaker(publication_engine)
    provider = FakeGithub()
    settings = Settings(
        github=Github(
            write_token=SecretStr("test-publication-write"), bot_user_id="9002"
        )
    )
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)

    acknowledged = await run_once(
        sessions,
        queue,
        settings=settings,
        owner="publisher-runtime",
        transport=httpx.MockTransport(provider.respond),
    )

    assert acknowledged is True
    state = await PostgresPublicationStore(sessions).get(uuid.UUID(command.review_id))
    assert state is not None and state.status == "PUBLISHED"
    assert len(provider.posts) == 1 and len(provider.posts[0]["comments"]) == 1
    pending = await redis_client.xpending(f"{stream_prefix}:publish", "review-workers")
    assert pending["pending"] == 0
    assert await redis_client.xlen(f"{stream_prefix}:analyze") == 0


async def test_continuous_runtime_parks_unknown_without_retry_or_ack(
    cli_services: dict[str, str], payload_data: dict[str, Any]
) -> None:
    url = make_url(os.environ["DMC268_TEST_DATABASE_URL"]).set(
        database=cli_services["POSTGRES__DB"]
    )
    engine = create_async_engine(url)
    try:
        command = await publication_command(engine, payload_data)
    finally:
        await engine.dispose()
    environment = os.environ | cli_services
    root = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(root / "src"), str(root / "tests"))
    )
    script = dedent("""
        import asyncio
        import signal
        import httpx
        from test_github_publisher_postgres import FakeGithub
        from app.publication_worker import run

        provider = FakeGithub()
        provider.lose_post_response = True
        def respond(request):
            if request.method == "POST":
                asyncio.get_running_loop().call_later(
                    0.3, signal.raise_signal, signal.SIGTERM
                )
            return provider.respond(request)
        async def probe():
            await run(
                once=False, poll_interval=0.01,
                transport=httpx.MockTransport(respond)
            )
            print("PROBE_POSTS=" + str(len(provider.posts)))
        asyncio.run(probe())
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "PROBE_POSTS=1" in result.stdout
    assert '"event": "publication_worker_delivery_deferred"' in result.stdout
    assert "provider-secret" not in result.stdout + result.stderr
    assert "private-test-write" not in result.stdout + result.stderr
    async with Redis.from_url(
        cli_services["REDIS__URL"], decode_responses=True
    ) as client:
        pending = await client.xpending("dmc268:review-tasks:publish", "review-workers")
        assert pending["pending"] == 1
    engine = create_async_engine(url)
    try:
        state = await PostgresPublicationStore(async_sessionmaker(engine)).get(
            uuid.UUID(command.review_id)
        )
        assert state is not None and state.status == "UNKNOWN"
    finally:
        await engine.dispose()


async def test_continuous_runtime_defers_expired_publication_authority(
    cli_services: dict[str, str], payload_data: dict[str, Any]
) -> None:
    url = make_url(os.environ["DMC268_TEST_DATABASE_URL"]).set(
        database=cli_services["POSTGRES__DB"]
    )
    engine = create_async_engine(url)
    try:
        await publication_command(engine, payload_data)
    finally:
        await engine.dispose()
    environment = os.environ | cli_services
    root = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(root / "src"), str(root / "tests"))
    )
    script = dedent("""
        import asyncio
        import signal
        import httpx
        from sqlalchemy import func, update
        from test_github_publisher_postgres import FakeGithub
        from app.db.models import TaskLease
        from app.db.session import async_session_factory
        from app.publication_worker import run

        provider = FakeGithub()
        async def respond(request):
            response = provider.respond(request)
            if request.method == "POST":
                async with async_session_factory.begin() as session:
                    await session.execute(
                        update(TaskLease).values(expires_at=func.clock_timestamp())
                    )
                asyncio.get_running_loop().call_later(
                    0.3, signal.raise_signal, signal.SIGTERM
                )
            return response
        async def probe():
            await run(
                once=False, poll_interval=0.01,
                transport=httpx.MockTransport(respond)
            )
            print("PROBE_POSTS=" + str(len(provider.posts)))
        asyncio.run(probe())
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "PROBE_POSTS=1" in result.stdout
    assert '"error_type": "PublicationAuthorityError"' in result.stdout
    async with Redis.from_url(
        cli_services["REDIS__URL"], decode_responses=True
    ) as client:
        pending = await client.xpending("dmc268:review-tasks:publish", "review-workers")
        assert pending["pending"] == 1
