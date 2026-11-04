"""GitHub webhook intake endpoint."""

import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from starlette.responses import JSONResponse
from structlog.contextvars import get_contextvars

from app.application.github_ports import GithubPort, GithubRequest
from app.application.use_cases.process_webhook import process_webhook
from app.application.webhook_ports import InboxUnavailable, WebhookInbox
from app.core.config import Settings, get_settings
from app.core.github_runtime import github_connection
from app.db.session import async_session_factory
from app.db.webhook_inbox import SqlAlchemyWebhookInbox

router = APIRouter()
_MAX_BODY_BYTES = 25 * 1024 * 1024
_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+\Z")


def get_webhook_inbox() -> WebhookInbox:
    return SqlAlchemyWebhookInbox(async_session_factory)


async def get_github_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[GithubPort]:
    async with github_connection(settings) as github:
        yield github


def _validation_error() -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_FAILED",
            "message": "Webhook request is invalid.",
            "request_id": get_contextvars()["request_id"],
            "retryable": False,
            "retry_after": None,
        },
    )


async def parse_github_event(payload: dict[str, Any]) -> GithubRequest | None:
    action = payload.get("action")
    if not isinstance(action, str):
        raise ValueError("invalid pull request action")
    if action not in {"opened", "synchronize"}:
        return None
    pull = payload.get("pull_request")
    number = payload.get("number")
    if (
        not isinstance(pull, dict)
        or type(number) is not int
        or number < 1
        or type(pull.get("number")) is not int
        or pull["number"] != number
    ):
        raise ValueError("invalid pull request number")
    repository = payload.get("repository")
    head = pull.get("head")
    base = pull.get("base")
    if (
        not isinstance(repository, dict)
        or not isinstance(head, dict)
        or not isinstance(base, dict)
        or not isinstance(head.get("repo"), dict)
        or not isinstance(base.get("repo"), dict)
    ):
        raise ValueError("invalid pull request repository")
    repository_id = repository.get("id")
    base_id = base["repo"].get("id")
    head_id = head["repo"].get("id")
    if (
        type(repository_id) is not int
        or repository_id < 1
        or type(base_id) is not int
        or base_id != repository_id
        or type(head_id) is not int
        or head_id < 1
    ):
        raise ValueError("invalid pull request repository ids")
    if pull.get("state") != "open":
        raise ValueError("pull request is not open")
    if any(
        not isinstance(side.get("sha"), str)
        or not _SHA.fullmatch(side["sha"])
        or not isinstance(side.get("ref"), str)
        or not side["ref"].strip()
        for side in (head, base)
    ):
        raise ValueError("invalid pull request refs")
    owner = repository.get("owner")
    name = repository.get("name")
    if (
        not isinstance(owner, dict)
        or not isinstance(owner.get("login"), str)
        or not _OWNER.fullmatch(owner["login"])
        or not isinstance(name, str)
        or not _REPOSITORY.fullmatch(name)
        or name in {".", ".."}
    ):
        raise ValueError("invalid repository name")
    return GithubRequest(
        owner=owner["login"],
        name=name,
        repository_external_id=str(repository_id),
        number=number,
        head_sha=head["sha"],
        base_sha=base["sha"],
    )


@router.post("/api/v1/webhooks/github")
async def github_webhook(  # noqa: PLR0911 - explicit guarded HTTP exits
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    inbox: Annotated[WebhookInbox, Depends(get_webhook_inbox)],
    github: Annotated[GithubPort, Depends(get_github_client)],
    background: BackgroundTasks,
) -> JSONResponse:
    body_chunks: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > _MAX_BODY_BYTES:
            return _validation_error()
        body_chunks.append(chunk)
    body = b"".join(body_chunks)
    secret = settings.webhook.secret.get_secret_value()
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    signature = request.headers.get("x-hub-signature-256", "")
    if secret and signature.isascii() and hmac.compare_digest(signature, expected):
        if not re.fullmatch(
            r"[A-Za-z0-9-]{1,128}", request.headers.get("x-github-delivery", "")
        ) or not re.fullmatch(
            r"[a-z_]{1,64}", request.headers.get("x-github-event", "")
        ):
            return _validation_error()
        if request.headers["x-github-event"] == "pull_request":
            try:
                payload = json.loads(body.decode("utf-8"))
            except ValueError:
                return _validation_error()
            if not isinstance(payload, dict):
                return _validation_error()
            try:
                command = await parse_github_event(payload)
            except ValueError:
                return _validation_error()
            if command is not None:
                try:
                    receipt_id = await inbox.accept(
                        request.headers["x-github-delivery"], command
                    )
                except InboxUnavailable:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "code": "SERVICE_UNAVAILABLE",
                            "message": "Service is temporarily unavailable.",
                            "request_id": get_contextvars()["request_id"],
                            "retryable": True,
                            "retry_after": 30,
                        },
                    )
                if receipt_id is not None:
                    background.add_task(process_webhook, receipt_id, inbox, github)
        return JSONResponse(status_code=202, content=None)
    return JSONResponse(
        status_code=401,
        content={
            "code": "AUTHENTICATION_REQUIRED",
            "message": "Authentication is required.",
            "request_id": get_contextvars()["request_id"],
            "retryable": False,
            "retry_after": None,
        },
    )
