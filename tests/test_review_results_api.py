from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio
import yaml
from fastapi import FastAPI
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import delete, insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_postgres_integration import (
    PostgresHarness,
    ReviewScope,
    _create_change_request,
    _create_repository,
    _create_settings,
    _review_values,
)

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.db import models
from app.db.session import get_session
from app.main import create_app

pytest_plugins = ("test_postgres_integration",)
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def assert_schema(response: httpx.Response, name: str) -> None:
    document = yaml.safe_load(Path("openapi.yaml").read_text())
    schema = {"$ref": f"#/components/schemas/{name}", **document}
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        response.json()
    )


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json() == {
        "code": code,
        "message": {
            401: "Authentication is required.",
            404: "Resource was not found.",
            422: "Request is invalid.",
        }[status],
        "request_id": response.headers["X-Request-Id"],
        "retryable": False,
        "retry_after": None,
    }
    assert_schema(response, "Error")


@pytest_asyncio.fixture
async def app(postgres: PostgresHarness) -> AsyncIterator[FastAPI]:
    application = create_app()
    factory = async_sessionmaker(postgres.engine)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as value:
            yield value

    application.dependency_overrides[get_session] = session
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


@pytest_asyncio.fixture
async def review(postgres: PostgresHarness) -> AsyncIterator[uuid.UUID]:
    async with postgres.engine.begin() as connection:
        repo = await _create_repository(postgres, connection)
        settings = await _create_settings(postgres, connection, repo)
        change = await _create_change_request(postgres, connection, repo)
        await connection.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == change)
            .values(external_number="13")
        )
        review_id = postgres.track("review_job", uuid.uuid4())
        await connection.execute(
            insert(models.ReviewJob).values(
                **{
                    **_review_values(review_id, ReviewScope(repo, change, settings)),
                    "created_at": NOW,
                    "queue_deadline_at": NOW + timedelta(minutes=15),
                }
            )
        )
        publication = postgres.track("publication", uuid.uuid4())
        await connection.execute(
            insert(models.Publication).values(
                id=publication,
                review_job_id=review_id,
                status="NOT_READY",
                provider_metadata={"credential": "secret-provider-data"},
                created_at=NOW,
            )
        )
    yield review_id
    async with postgres.engine.begin() as connection:
        chunks = models.ChunkResult.__table__
        chunk_ids = (
            chunks.select()
            .with_only_columns(chunks.c.id)
            .where(chunks.c.review_job_id == review_id)
        )
        await connection.execute(
            delete(models.Finding).where(models.Finding.chunk_result_id.in_(chunk_ids))
        )
        for table in (models.ChunkResult, models.ContextPayload, models.ReviewEvent):
            await connection.execute(
                delete(table).where(table.review_job_id == review_id)
            )


async def test_anonymous_detail_is_fail_closed(
    client: httpx.AsyncClient,
    review: uuid.UUID,
) -> None:
    response = await client.get(
        f"/api/v1/reviews/{review}",
        params={"user_id": str(uuid.uuid4())},
        headers={"X-User-Id": str(uuid.uuid4()), "Cookie": "session=arbitrary"},
    )
    assert_error(response, 401, "AUTHENTICATION_REQUIRED")


@pytest_asyncio.fixture
async def principal(
    app: FastAPI, postgres: PostgresHarness, review: uuid.UUID
) -> AsyncIterator[uuid.UUID]:

    user = uuid.uuid4()
    async with postgres.engine.begin() as connection:
        await connection.execute(insert(models.User).values(id=user))
        repo = (
            await connection.execute(
                models.ReviewJob.__table__.select()
                .with_only_columns(models.ReviewJob.repository_id)
                .where(models.ReviewJob.id == review)
            )
        ).scalar_one()
        await connection.execute(
            insert(models.RepositoryAccess).values(
                id=uuid.uuid4(), repository_id=repo, user_id=user, role="reviewer"
            )
        )
    app.dependency_overrides[get_authenticated_principal] = lambda: (
        AuthenticatedPrincipal(user)
    )
    yield user
    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.user_id == user
            )
        )
        await connection.execute(delete(models.User).where(models.User.id == user))


async def test_queued_detail_preserves_unavailable_values(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    response = await client.get(f"/api/v1/reviews/{review}")
    assert response.status_code == HTTPStatus.OK
    async with postgres.engine.connect() as connection:
        repo = (
            await connection.execute(
                models.ReviewJob.__table__.select()
                .with_only_columns(models.ReviewJob.repository_id)
                .where(models.ReviewJob.id == review)
            )
        ).scalar_one()
    assert response.json() == {
        "id": str(review),
        "repository_id": str(repo),
        "pull_request_number": 13,
        "status": "QUEUED",
        "requested_head_sha": "head-sha",
        "snapshot": None,
        "progress": {"completed_chunks": 0, "planned_chunks": 0},
        "summary": None,
        "coverage": None,
        "usage": None,
        "publication_status": "NOT_READY",
        "created_at": "2026-10-09T12:00:00Z",
        "started_at": None,
        "finished_at": None,
        "retry_at": None,
        "error": None,
    }
    assert_schema(response, "ReviewDetail")


async def test_access_denied_and_absent_review_have_identical_safe_404(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.user_id == principal
            )
        )
    denied = await client.get(f"/api/v1/reviews/{review}")
    absent = await client.get(f"/api/v1/reviews/{uuid.uuid4()}")
    assert_error(denied, 404, "RESOURCE_NOT_FOUND")
    assert_error(absent, 404, "RESOURCE_NOT_FOUND")


COVERAGE: dict[str, Any] = {
    "included": [{"path": "src/a.py", "ranges": [{"start_line": 1, "end_line": 2}]}],
    "skipped": [{"path": "vendor/b.py", "ranges": [], "reason": "policy"}],
    "context_quality": "diff_only",
}


async def save_chunk(
    postgres: PostgresHarness, review: uuid.UUID, usage: dict[str, Any] | None = None
) -> uuid.UUID:
    context = postgres.track("context_payload", uuid.uuid4())
    chunk = postgres.track("chunk_result", uuid.uuid4())
    async with postgres.engine.begin() as connection:
        await connection.execute(
            insert(models.ContextPayload).values(
                id=context,
                review_job_id=review,
                schema_version=1,
                payload_body={
                    "prompt": "secret-prompt",
                    "budget": {"estimated_input_tokens": 999},
                },
                created_at=NOW,
            )
        )
        await connection.execute(
            insert(models.ChunkResult).values(
                id=chunk,
                review_job_id=review,
                context_payload_id=context,
                schema_version=1,
                summary="Chunk summary",
                limitations=[],
                usage=usage,
                latency_ms=7,
                created_at=NOW,
            )
        )
    return chunk


async def test_completed_detail_reports_saved_snapshot_coverage_progress_and_usage(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    await save_chunk(postgres, review, {"input_tokens": 100, "output_tokens": 40})
    await save_chunk(postgres, review, {"input_tokens": 20, "output_tokens": 5})
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == review)
            .values(
                status="COMPLETED",
                final_summary="Fix division.",
                coverage=COVERAGE,
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                base_sha="b" * 40,
                merge_base_sha="c" * 40,
                captured_at=NOW,
                provider_diff_version={"secret": "secret-provider-data"},
            )
        )
    response = await client.get(f"/api/v1/reviews/{review}")
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["snapshot"] == {
        "base_sha": "b" * 40,
        "merge_base_sha": "c" * 40,
        "head_sha": "head-sha",
        "captured_at": "2026-10-09T12:00:00Z",
    }
    assert data["progress"] == {"completed_chunks": 2, "planned_chunks": 2}
    assert data["summary"] == "Fix division."
    assert data["coverage"] == COVERAGE
    assert data["usage"] == {"input_tokens": 120, "output_tokens": 45}
    assert data["publication_status"] == "NOT_READY"
    assert data["status"] == "COMPLETED"
    assert data["error"] is None
    assert "secret" not in response.text and "prompt" not in response.text
    assert_schema(response, "ReviewDetail")


async def test_lifecycle_errors_are_safe_current_and_independent_of_publication(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    for status, reason, retryable, publication in (
        ("FAILED", "LLM_OUTPUT_INVALID", False, "FAILED"),
        ("SKIPPED", "PR_STALE_OR_CLOSED", False, "SKIPPED"),
        ("PARTIAL", "ANALYSIS_DEADLINE_EXCEEDED", False, "PUBLISHED"),
        ("LLM_PROCESSING", "LLM_UNAVAILABLE", True, "NOT_READY"),
    ):
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(models.ReviewEvent).where(
                    models.ReviewEvent.review_job_id == review
                )
            )
            await connection.execute(
                update(models.ReviewJob)
                .where(models.ReviewJob.id == review)
                .values(
                    status=status,
                    finished_at=None if retryable else NOW,
                    retry_at=NOW + timedelta(seconds=30) if retryable else None,
                )
            )
            await connection.execute(
                update(models.Publication)
                .where(models.Publication.review_job_id == review)
                .values(status=publication)
            )
            await connection.execute(
                insert(models.ReviewEvent).values(
                    id=uuid.uuid4(),
                    review_job_id=review,
                    occurred_at=NOW,
                    event_type="STATUS_CHANGED",
                    status=status,
                    reason_code=reason,
                    retryable=retryable,
                    safe_details={"raw_response": "secret-provider-data"},
                )
            )
        response = await client.get(f"/api/v1/reviews/{review}")
        data = response.json()
        assert data["error"] == {
            "reason_code": reason,
            "retryable": retryable,
            "retry_at": "2026-10-09T12:00:30Z" if retryable else None,
        }
        assert data["status"] == status and data["publication_status"] == publication
        assert all(
            data[key] is None for key in ("snapshot", "summary", "coverage", "usage")
        )
        assert "secret-provider-data" not in response.text
        assert_schema(response, "ReviewDetail")
    async with postgres.engine.begin() as connection:
        await connection.execute(
            insert(models.ReviewEvent).values(
                id=uuid.uuid4(),
                review_job_id=review,
                occurred_at=NOW + timedelta(seconds=1),
                event_type="RETRY",
                status="LLM_PROCESSING",
                reason_code="LEGACY_SECRET_REASON",
                retryable=True,
            )
        )
    unknown = await client.get(f"/api/v1/reviews/{review}")
    assert unknown.json()["error"] is None
    assert "LEGACY_SECRET_REASON" not in unknown.text
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == review)
            .values(status="COMPLETED", finished_at=NOW, retry_at=None)
        )
    assert (await client.get(f"/api/v1/reviews/{review}")).json()["error"] is None


async def test_coverage_projects_only_contract_fields_from_historical_json(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    coverage = {
        **COVERAGE,
        "provider_metadata": {"token": "secret-provider-data"},
        "included": [
            {**COVERAGE["included"][0], "raw_response": "secret-provider-data"}
        ],
    }
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == review)
            .values(coverage=coverage, base_sha="partial-snapshot")
        )
    response = await client.get(f"/api/v1/reviews/{review}")
    assert response.json()["coverage"] == COVERAGE
    assert response.json()["snapshot"] is None
    assert "secret-provider-data" not in response.text
    assert_schema(response, "ReviewDetail")
    for invalid in (
        {**COVERAGE, "context_quality": "internal"},
        {**COVERAGE, "included": [{"path": "../secret", "ranges": []}]},
        {
            **COVERAGE,
            "included": [
                {"path": "a.py", "ranges": [{"start_line": 3, "end_line": 1}]}
            ],
        },
        {
            **COVERAGE,
            "skipped": [{"path": "a.py", "ranges": [], "reason": "legacy-secret"}],
        },
    ):
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(models.ReviewJob)
                .where(models.ReviewJob.id == review)
                .values(coverage=invalid)
            )
        invalid_response = await client.get(f"/api/v1/reviews/{review}")
        assert invalid_response.json()["coverage"] is None
        assert_schema(invalid_response, "ReviewDetail")


async def test_usage_is_unknown_if_any_saved_chunk_lacks_trusted_token_counts(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    await save_chunk(postgres, review, {"input_tokens": 100, "output_tokens": 40})
    chunk = await save_chunk(
        postgres, review, {"input_tokens": True, "output_tokens": 5}
    )
    for usage in (
        {"input_tokens": True, "output_tokens": 5},
        {"input_tokens": -1, "output_tokens": 5},
        {"input_tokens": 1.5, "output_tokens": 5},
        {"input_tokens": "20", "output_tokens": 5},
        {"estimated_input_tokens": 999},
        {"input_tokens": 20},
        None,
    ):
        async with postgres.engine.begin() as connection:
            await connection.execute(
                update(models.ChunkResult)
                .where(models.ChunkResult.id == chunk)
                .values(usage=usage)
            )
        response = await client.get(f"/api/v1/reviews/{review}")
        assert response.json()["usage"] is None
        assert_schema(response, "ReviewDetail")


async def test_progress_keeps_known_chunks_after_context_retention(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    await save_chunk(postgres, review, {"input_tokens": 10, "output_tokens": 5})
    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.ContextPayload).where(
                models.ContextPayload.review_job_id == review
            )
        )
    response = await client.get(f"/api/v1/reviews/{review}")
    assert response.json()["progress"] == {"completed_chunks": 1, "planned_chunks": 1}
    assert_schema(response, "ReviewDetail")


async def test_empty_findings_obey_auth_and_resource_policy(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
    app: FastAPI,
) -> None:
    response = await client.get(f"/api/v1/reviews/{review}/findings")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"items": [], "next_cursor": None}
    assert_schema(response, "FindingPage")
    assert_error(
        await client.get(f"/api/v1/reviews/{uuid.uuid4()}/findings"),
        404,
        "RESOURCE_NOT_FOUND",
    )
    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.user_id == principal
            )
        )
    assert_error(
        await client.get(f"/api/v1/reviews/{review}/findings"),
        404,
        "RESOURCE_NOT_FOUND",
    )

    del app.dependency_overrides[get_authenticated_principal]
    assert_error(
        await client.get(
            f"/api/v1/reviews/{review}/findings",
            params={"user_id": str(principal)},
            headers={"X-User-Id": str(principal)},
        ),
        401,
        "AUTHENTICATION_REQUIRED",
    )


async def save_findings(
    postgres: PostgresHarness, chunk: uuid.UUID, count: int
) -> list[uuid.UUID]:
    ids = [postgres.track("finding", uuid.uuid4()) for _ in range(count)]
    async with postgres.engine.begin() as connection:
        for finding in ids:
            await connection.execute(
                insert(models.Finding).values(
                    id=finding,
                    chunk_result_id=chunk,
                    fingerprint="fingerprint",
                    path="src/a.py",
                    side="NEW",
                    start_line=1,
                    end_line=2,
                    category="correctness",
                    severity="high",
                    title="Division by zero",
                    explanation="Always raises.",
                    evidence="return value / 0",
                    recommendation="Remove division.",
                    proposed_diff_fix=None,
                    created_at=NOW,
                )
            )
    return sorted(ids)


async def test_completed_findings_return_all_required_safe_fields(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    chunk = await save_chunk(
        postgres, review, {"input_tokens": 100, "output_tokens": 40}
    )
    ids = await save_findings(postgres, chunk, 1)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == review)
            .values(status="COMPLETED", finished_at=NOW)
        )
        await connection.execute(
            update(models.Publication)
            .where(models.Publication.review_job_id == review)
            .values(
                status="PUBLISHED",
                provider_metadata={
                    "comments": [
                        {
                            "finding_id": str(ids[0]),
                            "id": "unsafe-unaccepted-reference",
                            "token": "secret",
                        }
                    ]
                },
            )
        )
    response = await client.get(f"/api/v1/reviews/{review}/findings")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "items": [
            {
                "id": str(ids[0]),
                "fingerprint": "fingerprint",
                "path": "src/a.py",
                "side": "NEW",
                "start_line": 1,
                "end_line": 2,
                "category": "correctness",
                "severity": "high",
                "title": "Division by zero",
                "explanation": "Always raises.",
                "evidence": "return value / 0",
                "recommendation": "Remove division.",
                "proposed_diff_fix": None,
                "publication_reference": None,
            }
        ],
        "next_cursor": None,
    }
    assert_schema(response, "FindingPage")


async def test_findings_cursor_pages_are_stable_and_validate_opaque_tokens(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    chunk = await save_chunk(postgres, review)
    ids = await save_findings(postgres, chunk, 25)
    first = await client.get(f"/api/v1/reviews/{review}/findings")
    assert [item["id"] for item in first.json()["items"]] == list(map(str, ids[:20]))
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str) and cursor
    second = await client.get(
        f"/api/v1/reviews/{review}/findings", params={"cursor": cursor, "limit": 3}
    )
    again = await client.get(
        f"/api/v1/reviews/{review}/findings", params={"cursor": cursor, "limit": 3}
    )
    assert second.json() == again.json()
    assert [item["id"] for item in second.json()["items"]] == list(map(str, ids[20:23]))
    last = await client.get(
        f"/api/v1/reviews/{review}/findings",
        params={"cursor": second.json()["next_cursor"], "limit": 3},
    )
    assert [item["id"] for item in last.json()["items"]] == list(map(str, ids[23:]))
    assert last.json()["next_cursor"] is None
    exact = await client.get(f"/api/v1/reviews/{review}/findings", params={"limit": 25})
    assert (
        len(exact.json()["items"]) == len(ids) and exact.json()["next_cursor"] is None
    )
    for response in (first, second, last, exact):
        assert_schema(response, "FindingPage")
    for invalid in ("", "not-a-cursor", "a" * 10000, "eyJpZCI6MX0=", "!!!!"):
        assert_error(
            await client.get(
                f"/api/v1/reviews/{review}/findings", params={"cursor": invalid}
            ),
            422,
            "VALIDATION_FAILED",
        )


async def test_cursor_cannot_be_reused_or_rebound_to_another_review(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    local_ids = await save_findings(postgres, await save_chunk(postgres, review), 3)
    async with postgres.engine.begin() as connection:
        repo = await _create_repository(postgres, connection)
        settings = await _create_settings(postgres, connection, repo)
        change = await _create_change_request(postgres, connection, repo)
        other = postgres.track("review_job", uuid.uuid4())
        await connection.execute(
            insert(models.ReviewJob).values(
                **_review_values(other, ReviewScope(repo, change, settings))
            )
        )
        await connection.execute(
            insert(models.RepositoryAccess).values(
                id=uuid.uuid4(), repository_id=repo, user_id=principal, role="admin"
            )
        )
    foreign_ids = await save_findings(postgres, await save_chunk(postgres, other), 3)
    page = await client.get(f"/api/v1/reviews/{other}/findings", params={"limit": 1})
    foreign_cursor = page.json()["next_cursor"]
    reused = await client.get(
        f"/api/v1/reviews/{review}/findings", params={"cursor": foreign_cursor}
    )
    assert_error(reused, 422, "VALIDATION_FAILED")
    payload = json.loads(base64.urlsafe_b64decode(foreign_cursor))
    payload[1] = str(review)
    rebound = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    assert_error(
        await client.get(
            f"/api/v1/reviews/{review}/findings", params={"cursor": rebound}
        ),
        422,
        "VALIDATION_FAILED",
    )
    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.repository_id == repo
            )
        )
    denied = await client.get(
        f"/api/v1/reviews/{other}/findings", params={"cursor": foreign_cursor}
    )
    assert_error(denied, 404, "RESOURCE_NOT_FOUND")
    local = await client.get(f"/api/v1/reviews/{review}/findings")
    assert [item["id"] for item in local.json()["items"]] == list(map(str, local_ids))
    assert all(
        str(value) not in reused.text + denied.text + local.text
        for value in foreign_ids
    )


async def test_invalid_limits_are_canonical_validation_errors(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    for limit in ("0", "-1", "101", "oops", "1.5", "", "9" * 10000):
        response = await client.get(
            f"/api/v1/reviews/{review}/findings", params={"limit": limit}
        )
        assert_error(response, 422, "VALIDATION_FAILED")
    await save_findings(postgres, await save_chunk(postgres, review), 101)
    for page_limit, expected in ((1, 1), (100, 100)):
        response = await client.get(
            f"/api/v1/reviews/{review}/findings", params={"limit": page_limit}
        )
        assert len(response.json()["items"]) == expected
        assert isinstance(response.json()["next_cursor"], str)
        assert_schema(response, "FindingPage")


async def test_non_uuid_review_identifiers_are_safe_not_found(
    client: httpx.AsyncClient,
    principal: uuid.UUID,
) -> None:
    for suffix in ("", "/findings"):
        assert_error(
            await client.get(f"/api/v1/reviews/not-a-review{suffix}"),
            404,
            "RESOURCE_NOT_FOUND",
        )


async def test_detail_uses_one_committed_snapshot_during_result_write(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    chunk = await save_chunk(postgres, review)
    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.ChunkResult).where(models.ChunkResult.id == chunk)
        )
        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == review)
            .values(status="LLM_PROCESSING", started_at=NOW)
        )
    async with postgres.engine.connect() as writer:
        transaction = await writer.begin()
        response_task: asyncio.Task[httpx.Response] | None = None
        try:
            # Hold result reads until the atomic completion commits. The first
            # review SELECT can still observe the old committed state.
            await writer.execute(
                text("LOCK TABLE chunk_result IN ACCESS EXCLUSIVE MODE")
            )
            context = (
                await writer.execute(
                    models.ContextPayload.__table__.select()
                    .with_only_columns(models.ContextPayload.id)
                    .where(models.ContextPayload.review_job_id == review)
                )
            ).scalar_one()
            await writer.execute(
                insert(models.ChunkResult).values(
                    id=chunk,
                    review_job_id=review,
                    context_payload_id=context,
                    schema_version=1,
                    summary="Complete summary",
                    limitations=[],
                    usage={"input_tokens": 10, "output_tokens": 5},
                    latency_ms=1,
                    created_at=NOW,
                )
            )
            await writer.execute(
                update(models.ReviewJob)
                .where(models.ReviewJob.id == review)
                .values(
                    status="COMPLETED",
                    final_summary="Complete summary",
                    finished_at=NOW,
                )
            )
            response_task = asyncio.create_task(client.get(f"/api/v1/reviews/{review}"))
            async with postgres.engine.connect() as observer:
                async with asyncio.timeout(10):
                    while True:
                        await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                        if await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE wait_event_type = 'Lock' "
                                "AND query LIKE '%chunk_result%' "
                                "AND pid <> pg_backend_pid())"
                            )
                        ):
                            break
                        await asyncio.sleep(0.01)
            await transaction.commit()
            response = await response_task
        finally:
            if transaction.is_active:
                await transaction.rollback()
            if response_task is not None and not response_task.done():
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
    data = response.json()
    assert data["status"] == "LLM_PROCESSING"
    assert data["summary"] is None
    assert data["progress"] == {"completed_chunks": 0, "planned_chunks": 1}
    assert data["usage"] is None
    assert_schema(response, "ReviewDetail")
