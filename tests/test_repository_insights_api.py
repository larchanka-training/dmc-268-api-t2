"""Repository insights observed through authenticated HTTP requests."""

# ruff: noqa: PLR2004 -- worked examples and HTTP status assertions
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, insert, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_postgres_integration import (
    PostgresHarness,
    ReviewScope,
    _create_change_request,
    _create_repository,
    _create_settings,
    _review_values,
)
from test_review_results_api import assert_schema

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.db import models
from app.db.session import get_session
from app.main import create_app

pytest_plugins = ("test_postgres_integration",)


@dataclass
class Insights:
    client: httpx.AsyncClient
    app: FastAPI
    postgres: PostgresHarness
    scope: ReviewScope
    user: UUID

    def authenticate(self) -> None:
        self.app.dependency_overrides[get_authenticated_principal] = lambda: (
            AuthenticatedPrincipal(self.user)
        )

    @property
    def url(self) -> str:
        return f"/api/v1/repositories/{self.scope.repository_id}"

    async def review(self, **overrides: Any) -> UUID:
        review_id = self.postgres.track("review_job", uuid4())
        values = _review_values(review_id, self.scope, config_digest=str(review_id))
        values.update(overrides)
        async with self.postgres.engine.begin() as connection:
            await connection.execute(insert(models.ReviewJob).values(**values))
            await connection.execute(
                insert(models.Publication).values(
                    id=self.postgres.track("publication", uuid4()),
                    review_job_id=review_id,
                    status="NOT_READY",
                    provider_metadata={"secret": "must-never-be-returned"},
                )
            )
        return review_id

    async def attempt(self, review_id: UUID, **overrides: Any) -> None:
        now = datetime.now(UTC)
        values = {
            "id": uuid4(),
            "review_job_id": review_id,
            "chunk_id": uuid4(),
            "operation_id": uuid4(),
            "call_kind": "initial",
            "analysis_attempt": 1,
            "lease_owner": "test",
            "fencing_token": 1,
            "provider_profile_ref": "test",
            "model_ref": "model:test",
            "reserved_input_tokens": 200,
            "reserved_output_tokens": 50,
            "actual_input_tokens": None,
            "actual_output_tokens": None,
            "state": "RESERVED",
            "reserved_at": now,
        }
        values.update(overrides)
        if values.get("settled_at") is not None:
            values["reserved_at"] = values["settled_at"]
        async with self.postgres.engine.begin() as connection:
            await connection.execute(insert(models.ReviewLlmAttempt).values(**values))

    async def findings(self, review_id: UUID, categories: list[str]) -> None:
        chunk = self.postgres.track("chunk_result", uuid4())
        async with self.postgres.engine.begin() as connection:
            await connection.execute(
                insert(models.ChunkResult).values(
                    id=chunk,
                    review_job_id=review_id,
                    schema_version=1,
                    summary="test",
                    limitations=[],
                    latency_ms=1,
                )
            )
            for category in categories:
                finding = self.postgres.track("finding", uuid4())
                await connection.execute(
                    insert(models.Finding).values(
                        id=finding,
                        chunk_result_id=chunk,
                        fingerprint=str(finding),
                        path="a.py",
                        side="NEW",
                        start_line=1,
                        end_line=1,
                        category=category,
                        severity="high",
                        title="test",
                        explanation="test",
                        evidence="test",
                        recommendation="test",
                    )
                )


@pytest_asyncio.fixture
async def insights(postgres: PostgresHarness) -> AsyncIterator[Insights]:
    app = create_app()
    factory = async_sessionmaker(postgres.engine)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as value:
            yield value

    app.dependency_overrides[get_session] = session
    user = uuid4()
    async with postgres.engine.begin() as connection:
        repo = await _create_repository(postgres, connection)
        settings = await _create_settings(postgres, connection, repo)
        change = await _create_change_request(postgres, connection, repo)
        await connection.execute(insert(models.User).values(id=user))
        await connection.execute(
            insert(models.GithubIdentity).values(
                user_id=user,
                external_id=str(user.int),
                login="reviewer",
                encrypted_token="secret-credential",
            )
        )
        await connection.execute(
            insert(models.RepositoryAccess).values(
                id=uuid4(), repository_id=repo, user_id=user, role="reviewer"
            )
        )
        await connection.execute(
            update(models.Repository)
            .where(models.Repository.id == repo)
            .values(current_settings_id=settings)
        )
        await connection.execute(
            update(models.ChangeRequest)
            .where(models.ChangeRequest.id == change)
            .values(external_number="31")
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Insights(client, app, postgres, ReviewScope(repo, change, settings), user)
    app.dependency_overrides.clear()
    async with postgres.engine.begin() as connection:
        await connection.execute(
            delete(models.ReviewLlmAttempt).where(
                models.ReviewLlmAttempt.review_job_id.in_(
                    postgres.ids.get("review_job", set())
                )
            )
        )
        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.initiator_user_id == user)
            .values(initiator_user_id=None)
        )
        await connection.execute(
            delete(models.QuotaUsage).where(models.QuotaUsage.repository_id == repo)
        )
        await connection.execute(
            delete(models.RepositoryAccess).where(
                models.RepositoryAccess.user_id == user
            )
        )
        await connection.execute(
            delete(models.GithubIdentity).where(models.GithubIdentity.user_id == user)
        )
        await connection.execute(delete(models.User).where(models.User.id == user))


async def test_history_is_visible_only_with_repository_access(
    insights: Insights,
) -> None:
    review = await insights.review(initiator_user_id=insights.user)
    anonymous = await insights.client.get(f"{insights.url}/reviews")
    assert anonymous.status_code == 401
    insights.authenticate()
    response = await insights.client.get(f"{insights.url}/reviews")
    assert response.status_code == 200
    assert_schema(response, "ReviewListPage")
    row = response.json()["items"][0]
    assert row["id"] == str(review)
    assert row["actor"] == {"id": str(insights.user), "login": "reviewer"}
    assert row["trigger_type"] == "manual"
    assert row["pull_request_number"] == 31
    assert row["publication_status"] == "NOT_READY"
    assert response.json()["next_cursor"] is None
    assert "secret" not in response.text
    insights.app.dependency_overrides[get_authenticated_principal] = lambda: (
        AuthenticatedPrincipal(uuid4())
    )
    denied = await insights.client.get(f"{insights.url}/reviews")
    absent = await insights.client.get(f"/api/v1/repositories/{uuid4()}/reviews")
    assert denied.status_code == absent.status_code == 404
    assert denied.json()["code"] == absent.json()["code"] == "RESOURCE_NOT_FOUND"


async def test_history_filters_and_cursor_keep_a_bounded_stable_period(
    insights: Insights,
) -> None:
    insights.authenticate()
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    ids = [
        await insights.review(status="FAILED", created_at=now, finished_at=now)
        for _ in range(3)
    ]
    await insights.review(created_at=now)
    await insights.review(
        status="FAILED", created_at=now - timedelta(days=40), finished_at=now
    )
    params = {
        "days": "7",
        "status": "FAILED",
        "limit": "2",
        "pull_request_number": "31",
    }
    first = await insights.client.get(f"{insights.url}/reviews", params=params)
    assert first.status_code == 200
    assert [row["id"] for row in first.json()["items"]] == sorted(
        map(str, ids), reverse=True
    )[:2]
    cursor = first.json()["next_cursor"]
    assert cursor
    second = await insights.client.get(
        f"{insights.url}/reviews", params={**params, "cursor": cursor}
    )
    assert [row["id"] for row in second.json()["items"]] == sorted(
        map(str, ids), reverse=True
    )[2:]
    assert second.json()["next_cursor"] is None
    changed_scope = await insights.client.get(
        f"{insights.url}/reviews",
        params={**params, "cursor": cursor, "status": "QUEUED"},
    )
    assert changed_scope.status_code == 422
    dated = await insights.client.get(
        f"{insights.url}/reviews",
        params={
            "date_from": (now - timedelta(days=40)).date().isoformat(),
            "date_to": (now - timedelta(days=40)).date().isoformat(),
        },
    )
    assert len(dated.json()["items"]) == 1
    no_pr = await insights.client.get(
        f"{insights.url}/reviews", params={"pull_request_number": "32"}
    )
    assert no_pr.json()["items"] == []
    for invalid in (
        {"limit": "101"},
        {"limit": "0"},
        {"days": "1000"},
        {"status": "RUNNING"},
        {"date_from": "bad"},
        {"date_from": "2026-10-31", "date_to": "2026-10-01"},
        {"date_from": "2020-01-01", "date_to": "2026-10-01"},
        {"cursor": "bad"},
        {"pull_request_number": "0"},
    ):
        response = await insights.client.get(f"{insights.url}/reviews", params=invalid)
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_FAILED"


async def test_audit_accounts_all_attempts_and_keeps_unknown_usage_and_cost_explicit(
    insights: Insights,
) -> None:
    insights.authenticate()
    mixed = await insights.review()
    complete = await insights.review()
    legacy = await insights.review()
    later = datetime.now(UTC) + timedelta(seconds=1)
    await insights.attempt(
        mixed,
        state="SUCCEEDED",
        settled_at=later,
        actual_input_tokens=100,
        actual_output_tokens=20,
    )
    await insights.attempt(
        mixed,
        state="ERROR",
        settled_at=later,
        error_code="timeout",
        actual_input_tokens=15,
        actual_output_tokens=0,
    )
    await insights.attempt(
        mixed,
        state="ERROR",
        settled_at=later,
        error_code="unknown_usage",
        actual_input_tokens=7,
    )
    await insights.attempt(mixed)
    await insights.attempt(
        complete,
        state="SUCCEEDED",
        settled_at=later,
        actual_input_tokens=0,
        actual_output_tokens=0,
    )
    response = await insights.client.get(f"{insights.url}/reviews")
    rows = {row["id"]: row for row in response.json()["items"]}
    assert_schema(response, "ReviewListPage")
    assert rows[str(mixed)]["usage"] == {
        "input_tokens": 122,
        "output_tokens": 20,
        "reserved_input_tokens": 200,
        "reserved_output_tokens": 100,
        "status": "incomplete",
    }
    assert rows[str(complete)]["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "reserved_input_tokens": 0,
        "reserved_output_tokens": 0,
        "status": "complete",
    }
    assert rows[str(legacy)]["usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "reserved_input_tokens": 0,
        "reserved_output_tokens": 0,
        "status": "unavailable",
    }
    assert all(
        row["cost"] == {"amount": None, "currency": None, "status": "unavailable"}
        for row in rows.values()
    )


async def test_analytics_counts_run_findings_and_duration_without_join_duplication(
    insights: Insights,
) -> None:
    anonymous = await insights.client.get(f"{insights.url}/analytics")
    assert anonymous.status_code == 401
    insights.authenticate()
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    completed = await insights.review(
        created_at=now,
        started_at=now + timedelta(seconds=10),
        finished_at=now + timedelta(seconds=11),
        status="COMPLETED",
    )
    partial = await insights.review(
        created_at=now,
        started_at=now,
        finished_at=now + timedelta(seconds=3),
        status="PARTIAL",
    )
    failed = await insights.review(
        created_at=now,
        started_at=now,
        finished_at=now + timedelta(seconds=9),
        status="FAILED",
    )
    await insights.review(created_at=now)
    old = await insights.review(
        created_at=now - timedelta(days=40),
        started_at=now,
        finished_at=now,
        status="COMPLETED",
    )
    await insights.findings(completed, ["security", "security", "correctness"])
    await insights.findings(partial, ["performance"])
    await insights.findings(failed, ["maintainability"])
    await insights.findings(old, ["security"])
    start, end = (now - timedelta(days=1)).date().isoformat(), now.date().isoformat()
    response = await insights.client.get(
        f"{insights.url}/analytics", params={"date_from": start, "date_to": end}
    )
    assert response.status_code == 200
    assert_schema(response, "RepositoryAnalytics")
    assert response.json() == {
        "period": {"date_from": start, "date_to": end, "timezone": "UTC"},
        "totals": {"reviews": 4, "findings": 5},
        "findings_by_day": [
            {
                "date": start,
                "security": 0,
                "correctness": 0,
                "performance": 0,
                "maintainability": 0,
            },
            {
                "date": end,
                "security": 2,
                "correctness": 1,
                "performance": 1,
                "maintainability": 1,
            },
        ],
        "average_duration_ms": 2000.0,
        "duration_sample_count": 2,
    }
    empty = await insights.client.get(
        f"{insights.url}/analytics", params={"date_from": start, "date_to": start}
    )
    assert empty.json()["average_duration_ms"] is None
    assert empty.json()["duration_sample_count"] == 0
    assert empty.json()["totals"] == {"reviews": 0, "findings": 0}
    denied = await insights.client.get(f"/api/v1/repositories/{uuid4()}/analytics")
    assert denied.status_code == 404


async def test_quotas_report_existing_admission_limits_without_inventing_daily_tokens(
    insights: Insights,
) -> None:
    anonymous = await insights.client.get(f"{insights.url}/quotas")
    assert anonymous.status_code == 401
    insights.authenticate()
    await insights.review()
    await insights.review(status="LLM_PROCESSING")
    await insights.review(status="COMPLETED", finished_at=datetime.now(UTC))
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    async with insights.postgres.engine.begin() as connection:
        await connection.execute(
            update(models.RepositorySettings)
            .where(models.RepositorySettings.id == insights.scope.settings_id)
            .values(max_starts_per_hour=5, max_active_jobs=1)
        )
        for window, used in ((hour, 3), (hour - timedelta(hours=1), 99)):
            await connection.execute(
                insert(models.QuotaUsage).values(
                    id=uuid4(),
                    repository_id=insights.scope.repository_id,
                    user_id=insights.user,
                    type="starts",
                    value=used,
                    window_start=window,
                )
            )
    response = await insights.client.get(f"{insights.url}/quotas")
    assert response.status_code == 200
    assert_schema(response, "RepositoryQuotas")
    assert response.json() == {
        "starts_per_hour": {
            "limit": 5,
            "used": 3,
            "remaining": 2,
            "resets_at": (hour + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "scope": "repository_user",
        },
        "active_jobs": {"limit": 1, "used": 2, "remaining": 0, "scope": "repository"},
        "daily_tokens": {"status": "unavailable"},
    }
    denied = await insights.client.get(f"/api/v1/repositories/{uuid4()}/quotas")
    assert denied.status_code == 404


async def test_history_cursor_survives_anchor_status_transition(
    insights: Insights,
) -> None:
    insights.authenticate()
    await insights.review()
    await insights.review()
    params = {"status": "QUEUED", "limit": "1"}
    first = await insights.client.get(f"{insights.url}/reviews", params=params)
    anchor = first.json()["items"][0]["id"]
    async with insights.postgres.engine.begin() as connection:
        await connection.execute(
            update(models.ReviewJob)
            .where(models.ReviewJob.id == UUID(anchor))
            .values(status="COMPLETED", finished_at=datetime.now(UTC))
        )
    second = await insights.client.get(
        f"{insights.url}/reviews",
        params={**params, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["items"][0]["id"] != anchor
    assert second.json()["items"][0]["status"] == "QUEUED"


async def test_audit_exposes_only_safe_distinct_analysis_limit_reasons(
    insights: Insights,
) -> None:
    insights.authenticate()
    limited = await insights.review(
        coverage={
            "skipped": [
                {"reason": "token_budget", "path": "private/file.py"},
                {"reason": "file_limit"},
                {"reason": "line_limit"},
                {"reason": "file_limit"},
                {"reason": "secret_reason"},
                {"reason": {"invalid": "legacy"}},
            ]
        }
    )
    unlimited = await insights.review()
    response = await insights.client.get(f"{insights.url}/reviews")
    rows = {row["id"]: row for row in response.json()["items"]}
    assert rows[str(limited)]["limit_reasons"] == [
        "file_limit",
        "line_limit",
        "token_budget",
    ]
    assert rows[str(unlimited)]["limit_reasons"] == []
    assert "private/file.py" not in response.text
    assert "secret_reason" not in response.text
