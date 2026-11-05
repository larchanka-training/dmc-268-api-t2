"""Shared Sentry initialization for API and worker processes."""

import logging

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import (
    LoggingIntegration,
    ignore_logger,
    ignore_logger_for_sentry_logs,
)
from sentry_sdk.integrations.starlette import StarletteIntegration

from app.core.config import Settings


def configure_sentry(settings: Settings) -> None:
    """Enable Sentry when a DSN is configured."""
    dsn = settings.sentry.dsn.get_secret_value().strip()
    if not dsn:
        return
    # Manual stage spans intentionally omit URLs, statements and queue payloads.
    # INFO HTTP logs would otherwise reintroduce those values as breadcrumbs.
    for name in ("httpx", "httpcore", "sqlalchemy.engine"):
        for pattern in (name, f"{name}.*"):
            ignore_logger(pattern)
            ignore_logger_for_sentry_logs(pattern)
    # An explicit data_collection dict uses spec defaults for omitted
    # fields, which collect more than send_default_pii=False, and it
    # disables EventScrubber. Pin every category and keep locals off.
    pii_off_terms = ["forwarded", "-ip", "remote-", "via", "-user"]
    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=1.0,
        auto_enabling_integrations=False,
        data_collection={
            "user_info": False,
            "cookies": {"mode": "denylist", "terms": pii_off_terms},
            "http_headers": {
                "request": {"mode": "denylist", "terms": pii_off_terms},
            },
            "http_bodies": [],
            "url_query_params": {"mode": "off"},
            "graphql": {"document": False, "variables": False},
            "gen_ai": {"inputs": False, "outputs": False},
            "database_query_data": False,
            "queues": False,
            "stack_frame_variables": False,
        },
        integrations=[
            FastApiIntegration(),
            StarletteIntegration(),
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR,
                sentry_logs_level=logging.WARNING,
                capture_sentry_logs=True,
            ),
        ],
    )
