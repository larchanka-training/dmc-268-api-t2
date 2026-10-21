"""Shared Sentry initialization for API and worker processes."""

import logging

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

from app.core.config import Settings


def configure_sentry(settings: Settings) -> None:
    """Enable Sentry when a DSN is configured."""
    dsn = settings.sentry.dsn.get_secret_value().strip()
    if not dsn:
        return
    # An explicit data_collection dict uses spec defaults for omitted
    # fields, which collect more than send_default_pii=False, and it
    # disables EventScrubber. Pin every category and keep locals off.
    pii_off_terms = ["forwarded", "-ip", "remote-", "via", "-user"]
    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=1.0,
        data_collection={
            "user_info": False,
            "cookies": {"mode": "denylist", "terms": pii_off_terms},
            "http_headers": {
                "request": {"mode": "denylist", "terms": pii_off_terms},
            },
            "http_bodies": [],
            "url_query_params": {"mode": "denylist", "terms": pii_off_terms},
            "graphql": {"document": False, "variables": False},
            "gen_ai": {"inputs": False, "outputs": False},
            "database_query_data": False,
            "queues": False,
            "stack_frame_variables": False,
        },
        integrations=[
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR,
                sentry_logs_level=logging.WARNING,
                capture_sentry_logs=True,
            )
        ],
    )
