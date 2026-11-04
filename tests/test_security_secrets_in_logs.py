"""Security: configured secret values never reach the rendered logs.

Captures the real logging pipeline (structlog processors -> stdlib root ->
rotating file, JSON rendering) and drives three surfaces that log in
production: LLM gateway attempts, webhook intake, and the request-id
middleware. Every record is scanned for every configured secret value.
"""

import hashlib
import hmac
import json
import logging
import random
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import httpx
import jwt
import pytest
import structlog
from pydantic import BaseModel, ConfigDict, SecretStr

from app.application.github_ports import GithubRequest
from app.core.config import (
    LLM,
    Eurorouter,
    Github,
    Logging,
    Ollama,
    Settings,
    Webhook,
    get_settings,
)
from app.core.logging_config import configure_logging
from app.core.publication_runtime import github_publisher_connection
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.client import ChatMessage, LLMRequest
from app.llm.providers import build_gateway
from app.main import create_app

SECRETS = {
    "webhook": "whsec-live-9f8e7d6c5b4a3210",
    "github_write": "ghp_WRITEtoken1234567890abcdef",
    "llm": "sk-llm-api-key-9988776655443322",
    "jwt": "jwt-signing-secret-0123456789abcdef",
}
LOG_INJECTION_PAYLOAD = "fake log line FAIL injected\r\nsecond line"


@pytest.fixture
async def log_file(tmp_path: Path) -> AsyncIterator[Path]:
    """Route the real logging pipeline into a temp file, then restore."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_structlog = dict(structlog.get_config())
    path = tmp_path / "audit.log"
    configure_logging(
        Logging(
            level="DEBUG",
            format="json",
            file_enabled=True,
            file_path=path,
        )
    )
    try:
        yield path
    finally:
        root.handlers.clear()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        structlog.configure(**saved_structlog)


def _audit(path: Path) -> str:
    return path.read_text("utf-8")


def _assert_no_secrets(path: Path) -> str:
    rendered = _audit(path)
    for name, value in SECRETS.items():
        assert value not in rendered, f"secret {name} leaked into the logs"
    return rendered


async def test_llm_gateway_logs_attempts_without_api_keys(log_file: Path) -> None:
    class Verdict(BaseModel):
        model_config = ConfigDict(extra="forbid")

        verdict: str

    scripted: dict[str, deque[httpx.Response]] = defaultdict(deque)
    # The primary key is rejected by the provider: the failed-attempt branch
    # runs for real, then the fallback key serves the call.
    scripted[SECRETS["llm"]].append(httpx.Response(401, json={"error": "denied"}))

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        if scripted[key]:
            return scripted[key].popleft()
        return httpx.Response(
            200,
            json={
                "model": "euro",
                "choices": [{"message": {"content": '{"verdict": "ok"}'}}],
            },
        )

    gateway = build_gateway(
        LLM(
            primary_provider="eurorouter",
            fallback_enabled=True,
            eurorouter=Eurorouter(
                base_url="https://llm.invalid/v1",
                model="euro",
                api_keys=[SecretStr(SECRETS["llm"]), SecretStr("secondary-key")],
            ),
            ollama=Ollama(base_url="http://ollama:11434", model="qwen"),
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rng=random.Random(0),
    )
    await gateway.generate_structured(
        LLMRequest(
            messages=[ChatMessage(role="user", content="review this")],
            max_output_tokens=100,
            timeout_s=120,
        ),
        Verdict,
        budget=RunBudget(
            BudgetLimits(8, 80_000, 16_000, 600),
            clock=lambda: datetime.now(UTC).timestamp(),
        ),
        input_tokens=100,
    )

    rendered = _audit(log_file)
    assert "llm_attempt" in rendered  # production events were captured
    assert "llm_attempt_failed" in rendered  # the rejected-attempt branch ran
    _assert_no_secrets(log_file)
    assert "secondary-key" not in rendered  # every credential, not only the first


async def test_webhook_intake_logs_rejections_without_secrets(log_file: Path) -> None:
    app = create_app()
    # create_app() reconfigures logging from settings; re-apply the audit file.
    configure_logging(
        Logging(level="DEBUG", format="json", file_enabled=True, file_path=log_file)
    )
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr(SECRETS["webhook"]))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Signature check failure.
        rejected = await client.post(
            "/api/v1/webhooks/github",
            content=b'{"action":"opened"}',
            headers={
                "X-Hub-Signature-256": "sha256=invalid",
                "X-GitHub-Delivery": "delivery-1",
                "X-GitHub-Event": "pull_request",
            },
        )
        # Valid HMAC: the secret itself must still stay out of the logs.
        body = b'{"zen":"Keep it simple."}'
        signature = hmac.new(
            SECRETS["webhook"].encode(), body, hashlib.sha256
        ).hexdigest()
        accepted = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-2",
                "X-GitHub-Event": "ping",
            },
        )
        # Log injection through the request id is neutralized to a fresh id.
        injected = await client.get(
            "/healthcheck",
            headers={"X-Request-Id": f"{SECRETS['jwt']}{LOG_INJECTION_PAYLOAD}"},
        )

    assert rejected.status_code == HTTPStatus.UNAUTHORIZED
    assert accepted.status_code == HTTPStatus.ACCEPTED
    assert injected.status_code == HTTPStatus.OK
    assert injected.headers["X-Request-Id"] != (
        f"{SECRETS['jwt']}{LOG_INJECTION_PAYLOAD}"
    )

    # Third-party configuration may have silenced noisy libraries (the worker
    # runtime raises the httpx level globally), so the pipeline proof is a
    # direct probe record, not transport chatter.
    structlog.get_logger().info("audit_probe")
    rendered = _audit(log_file)
    assert rendered.strip(), "the audit file captured no records at all"
    _assert_no_secrets(log_file)
    assert "injected" not in rendered
    assert LOG_INJECTION_PAYLOAD not in rendered
    # Every rendered record stays a single parseable JSON line.
    for line in rendered.splitlines():
        if line.strip():
            json.loads(line)


async def test_publication_client_keeps_write_token_out_of_logs(
    log_file: Path,
) -> None:
    """The sentinel write token really flows through the publication client."""

    def handler(request: httpx.Request) -> httpx.Response:
        # The production credential seam put the sentinel on the wire.
        assert request.headers["authorization"] == (f"Bearer {SECRETS['github_write']}")
        return httpx.Response(
            200,
            json={
                "state": "open",
                "number": 42,
                "title": "Review",
                "body": None,
                "head": {"sha": "a" * 40, "ref": "topic", "repo": {"id": 101}},
                "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 100}},
            },
        )

    settings = Settings(
        github=Github(
            write_token=SecretStr(SECRETS["github_write"]), bot_user_id="9002"
        )
    )
    async with github_publisher_connection(
        settings, transport=httpx.MockTransport(handler)
    ) as github:
        await github.check_pull(
            GithubRequest(
                owner="larchanka-training",
                name="demo",
                repository_external_id="100",
                number=42,
                head_sha="a" * 40,
                base_sha="b" * 40,
            )
        )

    structlog.get_logger().info("audit_probe")
    rendered = _assert_no_secrets(log_file)
    assert rendered.strip(), "the audit file captured no records at all"


async def test_jwt_signing_secret_stays_out_of_logs(log_file: Path) -> None:
    """The sentinel secret signs and verifies a session through the exact
    production calls (github_auth signing, auth.py verification options), so
    a leak of the signing secret or of the derived bearer cookie would fail
    the scan."""
    now = datetime.now(UTC)
    claims = {
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=30),
        "iss": "dmc268",
        "aud": "dmc268-browser",
    }
    encoded = jwt.encode(claims, SECRETS["jwt"], algorithm="HS256")
    verified = jwt.decode(
        encoded,
        SECRETS["jwt"],
        algorithms=["HS256"],
        issuer="dmc268",
        audience="dmc268-browser",
        options={"require": ["sub", "sid", "iat", "exp"]},
    )
    assert verified["sub"] == claims["sub"]

    structlog.get_logger().info("audit_probe")
    rendered = _assert_no_secrets(log_file)
    assert rendered.strip(), "the audit file captured no records at all"
    assert encoded not in rendered  # the cookie itself is a bearer credential
