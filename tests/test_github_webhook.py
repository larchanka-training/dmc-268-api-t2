import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings, Webhook, get_settings
from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret")),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_invalid_signature_is_rejected_with_safe_error(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/webhooks/github",
        content=b"{}",
        headers={
            "X-Hub-Signature-256": "sha256=invalid",
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "pull_request",
        },
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json() == {
        "code": "AUTHENTICATION_REQUIRED",
        "message": "Authentication is required.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }


async def test_signed_ping_is_accepted(client: httpx.AsyncClient) -> None:
    body = b'{"zen":"Keep it simple."}'
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    response = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": f"sha256={signature}",
            "X-GitHub-Delivery": "delivery-ping",
            "X-GitHub-Event": "ping",
        },
    )
    assert response.status_code == HTTPStatus.ACCEPTED


async def test_non_string_pull_request_action_is_safe_validation_error() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )
    payload = _opened_payload()
    payload["action"] = ["opened"]
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-action-list",
                "X-GitHub-Event": "pull_request",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["request_id"] == response.headers["X-Request-Id"]


async def test_missing_delivery_header_is_validation_error(
    client: httpx.AsyncClient,
) -> None:
    body = b"{}"
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    response = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": f"sha256={signature}",
            "X-GitHub-Event": "ping",
        },
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize(
    "event,delivery", [(None, "delivery-1"), ("ping", "bad\tidentifier"), ("", "x")]
)
async def test_invalid_envelope_is_rejected(
    client: httpx.AsyncClient, event: str | None, delivery: str
) -> None:
    body = b"{}"
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Delivery": delivery,
    }
    if event is not None:
        headers["X-GitHub-Event"] = event
    response = await client.post(
        "/api/v1/webhooks/github", content=body, headers=headers
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_streamed_body_over_25_mib_is_rejected() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(26):
            yield b"x" * 1024 * 1024

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=chunks(),
            headers={"X-GitHub-Delivery": "delivery-large", "X-GitHub-Event": "ping"},
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {
        "code": "VALIDATION_FAILED",
        "message": "Webhook request is invalid.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }


async def test_published_github_hmac_vector_and_tampered_body() -> None:
    # https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("It's a Secret to Everybody"))
    )
    published_digest = (
        "757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    )
    headers = {
        "X-Hub-Signature-256": f"sha256={published_digest}",
        "X-GitHub-Delivery": "delivery-golden",
        "X-GitHub-Event": "ping",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.post(
            "/api/v1/webhooks/github", content=b"Hello, World!", headers=headers
        )
        tampered = await client.post(
            "/api/v1/webhooks/github", content=b"Hello, World!!", headers=headers
        )

    assert accepted.status_code == HTTPStatus.ACCEPTED
    assert tampered.status_code == HTTPStatus.UNAUTHORIZED
    assert tampered.json()["request_id"] == tampered.headers["X-Request-Id"]


async def test_empty_webhook_secret_never_accepts_signature() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr(""))
    )
    body = b"{}"
    signature = hmac.new(b"", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-empty-secret",
                "X-GitHub-Event": "ping",
            },
        )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("body", [b"\xff", b"{", b"[]"])
async def test_signed_pull_request_requires_utf8_json_object(body: bytes) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-bad-json",
                "X-GitHub-Event": "pull_request",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["request_id"] == response.headers["X-Request-Id"]


def _opened_payload() -> dict[str, object]:
    return {
        "action": "opened",
        "number": 7,
        "repository": {"id": 100, "name": "project", "owner": {"login": "team"}},
        "pull_request": {
            "number": 7,
            "state": "open",
            "head": {"sha": "b" * 40, "ref": "topic", "repo": {"id": 200}},
            "base": {"sha": "a" * 40, "ref": "main", "repo": {"id": 100}},
        },
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("number", True),
        ("number", 0),
        ("pull_request.number", "7"),
        ("pull_request.number", 8),
    ],
)
async def test_opened_pull_request_requires_matching_integer_numbers(
    field: str, value: object
) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )
    payload = _opened_payload()
    if field == "number":
        payload["number"] = value
    else:
        pull = payload["pull_request"]
        assert isinstance(pull, dict)
        pull["number"] = value
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-number",
                "X-GitHub-Event": "pull_request",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("repository", "id"), True),
        (("repository", "id"), "100"),
        (("pull_request", "base", "repo", "id"), 999),
        (("pull_request", "head", "repo", "id"), 0),
    ],
)
async def test_opened_pull_request_requires_consistent_repository_ids(
    path: tuple[str, ...], value: object
) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )
    payload = _opened_payload()
    node: dict[str, object] = payload
    for key in path[:-1]:
        next_node = node[key]
        assert isinstance(next_node, dict)
        node = next_node
    node[path[-1]] = value
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-repo-id",
                "X-GitHub-Event": "pull_request",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_supported_action_requires_open_pull_request() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )
    payload = _opened_payload()
    pull = payload["pull_request"]
    assert isinstance(pull, dict)
    pull["state"] = "closed"
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-closed",
                "X-GitHub-Event": "pull_request",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("side", "field", "value"),
    [
        ("head", "sha", "b" * 39),
        ("base", "sha", "../main"),
        ("head", "ref", ""),
        ("base", "ref", 123),
    ],
)
async def test_supported_action_requires_full_shas_and_refs(
    side: str, field: str, value: object
) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )
    payload = _opened_payload()
    pull = payload["pull_request"]
    assert isinstance(pull, dict)
    item = pull[side]
    assert isinstance(item, dict)
    item[field] = value
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-sha-ref",
                "X-GitHub-Event": "pull_request",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("field", "value"),
    [("owner", "../other"), ("owner", ""), ("name", "other/repo"), ("name", "..")],
)
async def test_supported_action_requires_safe_repository_name(
    field: str, value: str
) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )
    payload = _opened_payload()
    repository = payload["repository"]
    assert isinstance(repository, dict)
    if field == "owner":
        owner = repository["owner"]
        assert isinstance(owner, dict)
        owner["login"] = value
    else:
        repository["name"] = value
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": "delivery-owner",
                "X-GitHub-Event": "pull_request",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize("action", ["closed"])
async def test_signed_pull_request_actions_preserve_acceptance_contract(
    action: str,
) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        webhook=Webhook(secret=SecretStr("test-webhook-secret"))
    )
    payload = _opened_payload()
    payload["action"] = action
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Delivery": f"delivery-{action}",
                "X-GitHub-Event": "pull_request",
            },
        )

    assert response.status_code == HTTPStatus.ACCEPTED


async def test_signed_excessive_json_integer_is_safe(client: httpx.AsyncClient) -> None:
    body = b'{"number":' + b"9" * 5000 + b"}"
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    response = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Delivery": "delivery-json-limits",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "VALIDATION_FAILED"
