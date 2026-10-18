"""Personal Disconnect through real browser sessions and PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, insert, select, update
from test_postgres_integration import (
    ReviewScope,
    _create_change_request,
    _create_retained_result,
    _create_review_job,
)
from test_repositories_api import Catalog
from test_repository_onboarding_api import github_repository, mutation_headers
from test_repository_reader_wiring import browser_cookie

from app.core.config import Settings
from app.db import models

pytest_plugins = (
    "test_postgres_integration",
    "test_repositories_api",
    "test_repository_reader_wiring",
    "test_repository_onboarding_api",
)


def connection_url(repository_id: str) -> str:
    return f"/api/v1/repositories/{repository_id}/connection"


async def test_personal_disconnect_preserves_history_and_reconnect(
    onboarding: Catalog,
) -> None:
    payload = github_repository()
    onboarding.provider = lambda request: httpx.Response(200, json=payload)
    request_body = {"provider": "github", "external_id": str(payload["id"])}
    connected = await onboarding.client.post(
        "/api/v1/repositories/connect",
        json=request_body,
        headers=mutation_headers(onboarding.user),
    )
    assert connected.status_code == HTTPStatus.CREATED
    repository_id = uuid.UUID(connected.json()["id"])
    onboarding.postgres.track("repository", repository_id)
    other = await onboarding.client.post(
        "/api/v1/repositories/connect",
        json=request_body,
        headers=mutation_headers(onboarding.other),
    )
    assert other.status_code == HTTPStatus.CREATED
    async with onboarding.postgres.engine.begin() as conn:
        settings_id = await conn.scalar(
            select(models.Repository.current_settings_id).where(
                models.Repository.id == repository_id
            )
        )
        assert settings_id is not None
        onboarding.postgres.track("repository_settings", settings_id)
        await conn.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == settings_id)
            .values(output_language="en", surrounding_lines=9)
        )
        change_id = await _create_change_request(
            onboarding.postgres, conn, repository_id
        )
        job_id = await _create_review_job(
            onboarding.postgres,
            conn,
            ReviewScope(repository_id, change_id, settings_id),
        )
        context_id, chunk_id, finding_id = await _create_retained_result(
            onboarding.postgres, conn, job_id
        )
        publication_id = onboarding.postgres.track("publication", uuid.uuid4())
        await conn.execute(
            insert(models.Publication).values(
                id=publication_id,
                review_job_id=job_id,
                status="PUBLISHED",
                provider_metadata={"comment_id": 123},
            )
        )

    async def retained() -> list[Any]:
        # Durability is explicitly part of this contract, including records with
        # no HTTP read API (credentials, context and frozen settings).
        async with onboarding.postgres.engine.connect() as conn:
            return [
                (await conn.execute(select(entity).where(entity.id == row_id))).one()
                for entity, row_id in (
                    (models.Repository, repository_id),
                    (models.RepositorySettings, settings_id),
                    (models.ChangeRequest, change_id),
                    (models.ReviewJob, job_id),
                    (models.ContextPayload, context_id),
                    (models.ChunkResult, chunk_id),
                    (models.Finding, finding_id),
                    (models.Publication, publication_id),
                )
            ] + list(
                (
                    await conn.execute(
                        select(models.GithubIdentity)
                        .where(
                            models.GithubIdentity.user_id.in_(
                                [onboarding.user, onboarding.other]
                            )
                        )
                        .order_by(models.GithubIdentity.user_id)
                    )
                ).all()
            )

    before = await retained()

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Disconnect must not call GitHub")

    onboarding.provider = unexpected
    url = connection_url(str(repository_id))
    for _ in range(2):
        response = await onboarding.client.delete(
            url, headers=mutation_headers(onboarding.user)
        )
        assert response.status_code == HTTPStatus.NO_CONTENT
        assert response.content == b""
        assert "content-type" not in response.headers
    assert await retained() == before
    async with onboarding.postgres.engine.connect() as conn:
        access = (
            await conn.execute(
                select(
                    models.RepositoryAccess.user_id, models.RepositoryAccess.role
                ).where(models.RepositoryAccess.repository_id == repository_id)
            )
        ).all()
    assert [tuple(row) for row in access] == [(onboarding.other, "reviewer")]

    # The disconnected user's catalogue no longer exposes the repository.
    listed = await onboarding.client.get(
        "/api/v1/repositories", headers=browser_cookie(onboarding.user)
    )
    assert listed.json() == {"items": [], "next_cursor": None}

    # Reconnect must recheck permissions and take the new GitHub role.
    payload["permissions"]["admin"] = True
    observed: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.path)
        return httpx.Response(200, json=payload)

    onboarding.provider = provider
    reconnected = await onboarding.client.post(
        "/api/v1/repositories/connect",
        json=request_body,
        headers=mutation_headers(onboarding.user),
    )
    assert reconnected.status_code == HTTPStatus.CREATED
    assert reconnected.json()["id"] == str(repository_id)
    assert reconnected.json()["role"] == "admin"
    assert observed == [f"/repositories/{payload['id']}"]
    assert await retained() == before


@pytest.mark.parametrize(
    "failure", ["delete", "commit_before", "commit_after", "rollback"]
)
async def test_database_failure_is_safe_and_retry_is_idempotent(
    onboarding: Catalog,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = await onboarding.connect("octocat", "database-failure")
    engine = onboarding.postgres.engine.sync_engine
    dialect = engine.dialect
    original_commit, original_rollback = dialect.do_commit, dialect.do_rollback
    rollbacks: list[bool] = []

    def commit(connection: Any) -> None:
        if failure == "commit_after":
            original_commit(connection)
        raise OSError("private-database-detail")

    def rollback(connection: Any) -> None:
        original_rollback(connection)
        rollbacks.append(True)
        if failure == "rollback" and len(rollbacks) == 1:
            raise OSError("private-rollback-detail")

    def break_delete(
        connection: Any, cursor: Any, statement: str, parameters: Any, *args: Any
    ) -> tuple[str, Any]:
        if failure in {"delete", "rollback"} and statement.startswith(
            "DELETE FROM repository_access"
        ):
            statement = statement.replace(
                "repository_access.user_id", "repository_access.absent"
            )
        return statement, parameters

    with monkeypatch.context() as patch:
        patch.setattr(dialect, "do_rollback", rollback)
        if failure.startswith("commit"):
            patch.setattr(dialect, "do_commit", commit)
        event.listen(engine, "before_cursor_execute", break_delete, retval=True)
        try:
            response = await onboarding.client.delete(
                connection_url(repository["id"]),
                headers=mutation_headers(onboarding.user),
            )
        finally:
            event.remove(engine, "before_cursor_execute", break_delete)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service is unavailable.",
        "request_id": response.headers["X-Request-Id"],
        "retryable": True,
        "retry_after": None,
    }
    assert rollbacks, "Attempt rollback even when commit outcome is unknown"
    assert "private-database-detail" not in response.text + caplog.text
    assert "private-rollback-detail" not in response.text + caplog.text
    async with onboarding.postgres.engine.connect() as conn:
        access = await conn.scalar(
            select(models.RepositoryAccess.user_id).where(
                models.RepositoryAccess.repository_id == uuid.UUID(repository["id"])
            )
        )
    assert access == (None if failure == "commit_after" else onboarding.user)
    for _ in range(2):
        retried = await onboarding.client.delete(
            connection_url(repository["id"]), headers=mutation_headers(onboarding.user)
        )
        assert retried.status_code == HTTPStatus.NO_CONTENT
        assert retried.content == b""


async def test_uuid_contract_never_discloses_foreign_connections(
    onboarding: Catalog,
) -> None:
    invalid = await onboarding.client.delete(
        connection_url("not-a-uuid"), headers=mutation_headers(onboarding.user)
    )
    assert invalid.status_code == HTTPStatus.NOT_FOUND
    assert invalid.json()["code"] == "RESOURCE_NOT_FOUND"
    foreign = await onboarding.connect("private", "foreign", user=onboarding.other)

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Disconnect must not call GitHub")

    onboarding.provider = unexpected
    for repository_id in (foreign["id"], str(uuid.uuid4()), uuid.UUID(int=0).hex):
        for _ in range(2):
            response = await onboarding.client.delete(
                connection_url(repository_id), headers=mutation_headers(onboarding.user)
            )
            assert response.status_code == HTTPStatus.NO_CONTENT
            assert response.content == b""
    async with onboarding.postgres.engine.connect() as conn:
        assert (
            await conn.scalar(
                select(models.RepositoryAccess.user_id).where(
                    models.RepositoryAccess.repository_id == uuid.UUID(foreign["id"])
                )
            )
            == onboarding.other
        )


@pytest.mark.parametrize(
    ("condition", "status", "code"),
    [
        ("anonymous", 401, "AUTHENTICATION_REQUIRED"),
        ("forged_session", 401, "AUTHENTICATION_REQUIRED"),
        ("revoked_session", 401, "AUTHENTICATION_REQUIRED"),
        ("expired_session", 401, "AUTHENTICATION_REQUIRED"),
        ("missing_origin", 403, "ACCESS_DENIED"),
        ("wrong_origin", 403, "ACCESS_DENIED"),
        ("duplicate_origin", 403, "ACCESS_DENIED"),
        ("missing_csrf", 403, "ACCESS_DENIED"),
        ("wrong_csrf", 403, "ACCESS_DENIED"),
        ("duplicate_csrf", 403, "ACCESS_DENIED"),
        ("jwt_configuration", 503, "SERVICE_UNAVAILABLE"),
        ("origin_configuration", 503, "SERVICE_UNAVAILABLE"),
        ("invalid_credential", 204, None),
        ("invalid_credential_key", 204, None),
    ],
)
async def test_disconnect_inherits_browser_security_without_github_dependency(  # noqa: PLR0912 -- shared browser security matrix
    onboarding: Catalog,
    reader_settings: Settings,
    condition: str,
    status: int,
    code: str | None,
) -> None:
    """Characterize the existing shared security dependency on this route."""
    repository = await onboarding.connect("octocat", "security")
    headers = mutation_headers(onboarding.user)
    if condition == "anonymous":
        headers.pop("Cookie")
    elif condition == "forged_session":
        headers["Cookie"] = "dmc_session=forged"
        headers["X-User-Id"] = str(onboarding.user)
    elif condition in {"missing_origin", "missing_csrf"}:
        headers.pop("Origin" if condition == "missing_origin" else "X-CSRF-Token")
    elif condition == "wrong_origin":
        headers["Origin"] = "https://evil.test"
    elif condition == "wrong_csrf":
        headers["X-CSRF-Token"] = "forged"
    elif condition == "jwt_configuration":
        reader_settings.auth.jwt_secret = SecretStr("")
    elif condition == "origin_configuration":
        reader_settings.auth.frontend_origins = []
    elif condition == "invalid_credential_key":
        reader_settings.auth.credential_key = SecretStr("malformed")

    async with onboarding.postgres.engine.begin() as conn:
        if condition in {"revoked_session", "expired_session"}:
            value = datetime.now(UTC) - timedelta(minutes=1)
            await conn.execute(
                update(models.AuthSession)
                .where(models.AuthSession.user_id == onboarding.user)
                .values(
                    **{
                        "revoked_at"
                        if condition == "revoked_session"
                        else "expires_at": value
                    }
                )
            )
        if condition == "invalid_credential":
            await conn.execute(
                update(models.GithubIdentity)
                .where(models.GithubIdentity.user_id == onboarding.user)
                .values(encrypted_token="corrupted-saved-credential")
            )

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "Disconnect must never require GitHub or decrypt credentials"
        )

    onboarding.provider = unexpected
    header_items = list(headers.items())
    if condition.startswith("duplicate"):
        name = "Origin" if condition == "duplicate_origin" else "X-CSRF-Token"
        header_items.append((name, headers[name]))
    response = await onboarding.client.delete(
        connection_url(repository["id"]), headers=header_items
    )
    assert response.status_code == status
    if code is not None:
        assert response.json() == {
            "code": code,
            "message": {
                401: "Authentication is required.",
                403: "Access is denied.",
                503: "Service is unavailable.",
            }[status],
            "request_id": response.headers["X-Request-Id"],
            "retryable": status == HTTPStatus.SERVICE_UNAVAILABLE,
            "retry_after": None,
        }
    else:
        assert response.content == b""
    async with onboarding.postgres.engine.connect() as conn:
        access = await conn.scalar(
            select(models.RepositoryAccess.user_id).where(
                models.RepositoryAccess.repository_id == uuid.UUID(repository["id"])
            )
        )
    assert access == (None if status == HTTPStatus.NO_CONTENT else onboarding.user)
