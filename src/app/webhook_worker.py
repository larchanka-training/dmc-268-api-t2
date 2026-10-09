"""Recovery runner for durable GitHub intake, separate from review execution."""

import argparse
import asyncio

import structlog

from app.application.github_ports import GithubPort
from app.application.use_cases.process_webhook import process_webhook
from app.application.webhook_ports import WebhookInbox
from app.core.config import get_settings
from app.core.github_runtime import github_connection
from app.core.logging_config import configure_logging
from app.db.session import async_session_factory, engine
from app.db.webhook_inbox import SqlAlchemyWebhookInbox


async def run_once(inbox: WebhookInbox, github: GithubPort) -> int:
    pending = await inbox.pending()
    for receipt_id in pending:
        await process_webhook(receipt_id, inbox, github)
    return len(pending)


async def _run(*, once: bool) -> None:
    settings = get_settings()
    configure_logging(settings.logging)
    try:
        async with github_connection(settings) as github:
            while True:
                count = await run_once(
                    SqlAlchemyWebhookInbox(async_session_factory), github
                )
                structlog.get_logger().info("webhook_batch", selected=count)
                if once:
                    return
                await asyncio.sleep(5)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Process one due batch and exit."
    )
    args = parser.parse_args()
    asyncio.run(_run(once=args.once))


if __name__ == "__main__":
    main()
