"""Sentry is enabled only when a DSN is configured."""

import logging
import os
import subprocess
import sys
import textwrap
from http import HTTPStatus
from pathlib import Path

import pytest
import sentry_sdk
import yaml
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app

ROOT = Path(__file__).parents[1]

_DSN = "https://public@o0.ingest.sentry.io/1"


@pytest.fixture(autouse=True)
def _reset_sentry() -> object:
    sentry_sdk.init(dsn="")
    get_settings.cache_clear()
    yield
    sentry_sdk.init(dsn="")
    get_settings.cache_clear()


def test_missing_dsn_leaves_sentry_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local runs and CI do not report anywhere when SENTRY__DSN is empty."""
    monkeypatch.delenv("SENTRY__DSN", raising=False)
    get_settings.cache_clear()

    create_app()

    assert sentry_sdk.get_client().options["dsn"] == ""


def test_deploy_forwards_sentry_dsn_to_the_api_container() -> None:
    """Staging receives the DSN only when the GitHub secret is set."""
    services = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())[
        "services"
    ]
    for name in ("api", "worker", "outbox-dispatcher", "webhook-worker"):
        assert services[name]["environment"]["SENTRY__DSN"] == "${SENTRY__DSN:-}"
    for name in ("web", "db", "redis"):
        assert "SENTRY__DSN" not in services[name].get("environment", {})

    example = (ROOT / ".env.staging.example").read_text(encoding="utf-8")
    assert "SENTRY__DSN=" in example

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    runtime = next(
        step for step in steps if step.get("name") == "Write runtime environment"
    )
    assert runtime["env"]["SENTRY_DSN"] == "${{ secrets.SENTRY__DSN }}"
    assert "append_optional 'SENTRY__DSN'" in runtime["run"]


def test_configured_dsn_enables_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SENTRY__DSN value makes the running app report to that project."""
    monkeypatch.setenv("SENTRY__DSN", _DSN)
    get_settings.cache_clear()

    create_app()

    client = sentry_sdk.get_client()
    assert client.is_active()
    assert client.options["dsn"] == _DSN


def test_configured_dsn_traces_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracing is on so a slow request shows up in Sentry."""
    monkeypatch.setenv("SENTRY__DSN", _DSN)
    get_settings.cache_clear()

    create_app()

    assert sentry_sdk.get_client().options["traces_sample_rate"] == 1.0


def test_sentry_does_not_collect_request_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request and response bodies stay out of Sentry events."""
    monkeypatch.setenv("SENTRY__DSN", _DSN)
    get_settings.cache_clear()

    create_app()

    terms = ["forwarded", "-ip", "remote-", "via", "-user"]
    collection = sentry_sdk.get_client().options["data_collection"]
    assert collection == {
        "provided_by_user": True,
        "user_info": False,
        "cookies": {"mode": "denylist", "terms": terms},
        "http_headers": {"request": {"mode": "denylist", "terms": terms}},
        "http_bodies": [],
        "url_query_params": {"mode": "off"},
        "graphql": {"document": False, "variables": False},
        "gen_ai": {"inputs": False, "outputs": False},
        "database_query_data": False,
        "queues": False,
        "stack_frame_variables": False,
        "frame_context_lines": 5,
    }


def test_oauth_callback_code_is_not_sent_to_sentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GitHub callback's code and state never leave in a Sentry event."""
    oauth_code = "oauth-code-9f3a"
    oauth_state = "oauth-state-9f3a"
    payloads: list[str] = []

    class _Capture(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope: object) -> None:
            raw = envelope.serialize()  # type: ignore[attr-defined]
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            payloads.append(text)

    monkeypatch.setattr(
        "sentry_sdk.client.make_transport",
        lambda _options: _Capture(),
    )
    monkeypatch.setenv("SENTRY__DSN", _DSN)
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/auth/github/callback",
            params={"code": oauth_code, "state": oauth_state},
            follow_redirects=False,
        )
    sentry_sdk.flush()

    callback = [
        payload
        for payload in payloads
        if '"transaction":"/api/v1/auth/github/callback"' in payload
    ]
    assert response.status_code == HTTPStatus.SEE_OTHER
    assert len(callback) == 1
    assert oauth_code not in callback[0]
    assert oauth_state not in callback[0]


def test_warning_logs_reach_sentry_and_info_logs_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The five-second worker poll must not fill the free log allowance."""
    bodies: list[str] = []

    class _Capture(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope: object) -> None:
            for item in getattr(envelope, "items", []):
                if getattr(item, "type", None) != "log":
                    continue
                payload = item.payload.json
                if not isinstance(payload, dict):
                    continue
                for entry in payload.get("items", []):
                    body = entry.get("body")
                    if isinstance(body, str):
                        bodies.append(body)

    monkeypatch.setattr(
        "sentry_sdk.client.make_transport",
        lambda _options: _Capture(),
    )
    monkeypatch.setenv("SENTRY__DSN", _DSN)
    get_settings.cache_clear()
    create_app()
    logging.getLogger("dmc.sentry").info("poll tick")
    logging.getLogger("dmc.sentry").warning("queue stalled")
    sentry_sdk.flush()

    assert bodies == ["queue stalled"]


def _assert_entrypoint_enables_sentry(code: str) -> None:
    """Run entrypoint code and require the configured DSN.

    The script must stub the worker loop. CI has Postgres and Redis, and
    Linux installs the process signal handlers, so the real loop does not
    return before this timeout.
    """
    script = textwrap.dedent(code) + (
        "\nclient = sentry_sdk.get_client()\n"
        "dsn = client.options.get('dsn', '')\n"
        "client.close(timeout=0)\n"
        "print(dsn)\n"
    )
    env = os.environ.copy()
    env["SENTRY__DSN"] = _DSN
    env["LOGGING__FILE_ENABLED"] = "false"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=ROOT,
        env=env,
    )
    assert _DSN in completed.stdout, completed.stderr


def test_webhook_worker_enables_sentry_when_dsn_is_set() -> None:
    """webhook_worker._run initializes Sentry before the GitHub loop."""
    _assert_entrypoint_enables_sentry(
        """
        import asyncio

        import sentry_sdk

        import app.webhook_worker as worker

        class _Connection:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_args: object) -> bool:
                return False

        async def _stop(*_args: object, **_kwargs: object) -> int:
            return 0

        class _Engine:
            async def dispose(self) -> None:
                return None

        worker.github_connection = lambda _settings: _Connection()
        worker.run_once = _stop
        worker.engine = _Engine()

        try:
            asyncio.run(worker._run(once=True))
        except (Exception, SystemExit):
            pass
        """
    )


def test_analysis_worker_enables_sentry_when_dsn_is_set() -> None:
    """analysis_worker.main initializes Sentry before the one-shot loop."""
    _assert_entrypoint_enables_sentry(
        """
        import sys

        import sentry_sdk

        import app.analysis_worker as worker

        async def _stop(*_args: object, **_kwargs: object) -> None:
            return None

        worker.run = _stop
        sys.argv = ["analysis_worker", "--once"]
        try:
            worker.main()
        except (Exception, SystemExit):
            pass
        """
    )


def test_publication_worker_enables_sentry_when_dsn_is_set() -> None:
    """publication_worker.main initializes Sentry before the one-shot loop."""
    _assert_entrypoint_enables_sentry(
        """
        import sys

        import sentry_sdk

        import app.publication_worker as worker

        async def _stop(*_args: object, **_kwargs: object) -> None:
            return None

        worker.run = _stop
        sys.argv = ["publication_worker", "--once"]
        try:
            worker.main()
        except (Exception, SystemExit):
            pass
        """
    )


def test_outbox_dispatcher_enables_sentry_when_dsn_is_set() -> None:
    """outbox_dispatcher.main initializes Sentry before the one-shot loop."""
    _assert_entrypoint_enables_sentry(
        """
        import sys

        import sentry_sdk

        import app.outbox_dispatcher as worker

        async def _stop(*_args: object, **_kwargs: object) -> None:
            return None

        worker._run = _stop
        sys.argv = ["outbox_dispatcher", "--once"]
        try:
            worker.main()
        except (Exception, SystemExit):
            pass
        """
    )
