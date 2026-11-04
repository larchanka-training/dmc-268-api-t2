"""HTTP admission through the existing durable analysis pipeline."""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from jsonschema import Draft202012Validator, FormatChecker
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_handler import DIFF, DeterministicLlm
from test_contracts import SCHEMAS
from test_postgres_integration import PostgresHarness
from test_review_results_api import assert_schema
from test_review_start_api import BASE, HEAD, HEADERS, StartScope

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_worker import PostgresReviewWorker
from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.application.github_ports import GithubPort
from app.core import github_runtime
from app.core.config import Settings
from app.db import models
from app.vcs.github import GithubClient

pytest_plugins = ("test_review_start_api", "test_redis_queue")
MERGE = "c" * 40
SHA256_HEX_LENGTH = 64


def github_transport() -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if "/pulls/" in request.url.path:
            return httpx.Response(
                HTTPStatus.OK,
                json={
                    "number": 13,
                    "title": "Fix division",
                    "body": "Description",
                    "state": "open",
                    "head": {
                        "repo": {"id": 123456},
                        "sha": HEAD,
                        "ref": "feature/postgres-validation",
                    },
                    "base": {"repo": {"id": 123456}, "sha": BASE, "ref": "main"},
                },
            )
        if request.headers["accept"] == "application/vnd.github.diff":
            return httpx.Response(HTTPStatus.OK, text=DIFF)
        return httpx.Response(HTTPStatus.OK, json={"merge_base_commit": {"sha": MERGE}})

    return httpx.MockTransport(respond)


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("duplicate_findings", [False, True])
async def test_http_start_dispatches_analysis_and_exposes_durable_result(  # noqa: PLR0913, PLR0915 -- explicit integration boundaries
    *,
    client: httpx.AsyncClient,
    app: FastAPI,
    scope: StartScope,
    postgres: PostgresHarness,
    redis_client: Redis,
    stream_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_findings: bool,
) -> None:
    document = yaml.safe_load(Path("openapi.yaml").read_text())
    body = {"requested_head_sha": HEAD}
    Draft202012Validator(
        {"$ref": "#/components/schemas/StartReviewRequest", **document},
        format_checker=FormatChecker(),
    ).validate(body)
    app.dependency_overrides[get_authenticated_principal] = lambda: (
        AuthenticatedPrincipal(scope.user)
    )
    factory = async_sessionmaker(postgres.engine)
    queue = RedisQueue(redis_client, stream_prefix=stream_prefix)
    llm = DeterministicLlm()
    if duplicate_findings:
        first = llm.result.findings[0]
        llm.result = replace(
            llm.result,
            findings=[
                first,
                first,
                replace(first, explanation="Later duplicate explanation."),
                replace(first, title="Distinct issue at the same location"),
            ],
        )
    expected_count = 2 if duplicate_findings else 1

    async with httpx.AsyncClient(transport=github_transport()) as github_http:
        github = GithubClient(github_http)

        @asynccontextmanager
        async def gateway(settings: Settings) -> AsyncIterator[GithubPort]:
            yield github

        monkeypatch.setattr(github_runtime, "github_connection", gateway)
        response = await client.post(scope.url, json=body, headers=HEADERS)
        assert response.status_code == HTTPStatus.ACCEPTED
        assert_schema(response, "StartReviewResponse")
        review_id = uuid.UUID(response.json()["review_id"])
        resource_url = f"/api/v1/reviews/{review_id}"
        assert response.json()["resource_url"] == response.headers["Location"]
        assert response.headers["Location"] == resource_url
        assert response.json()["status"] == "QUEUED"
        assert await redis_client.xlen(f"{stream_prefix}:analyze") == 0
        async with factory() as session:
            outbox = (
                await session.scalars(
                    select(models.OutboxEvent).where(
                        models.OutboxEvent.review_job_id == review_id
                    )
                )
            ).one()
            assert outbox.task_kind == "analyze"
            assert outbox.broker_published_at is None
            event_id = str(outbox.id)

        queued = await client.get(resource_url)
        assert queued.status_code == HTTPStatus.OK
        assert queued.json()["status"] == "QUEUED"
        assert_schema(queued, "ReviewDetail")

        assert await PostgresOutboxDispatcher(factory, queue).dispatch_batch() == 1
        messages = await redis_client.xrange(f"{stream_prefix}:analyze")
        assert messages is not None and len(messages) == 1
        fields = messages[0][1]
        assert fields is not None
        task = json.loads(fields["envelope"])
        assert task["event_id"] == event_id
        assert task["review_id"] == str(review_id)
        assert task["task_kind"] == "analyze"

        handler = PostgresAnalysisHandler(factory, github, llm, schemas_dir=SCHEMAS)
        worker = PostgresReviewWorker(
            factory, queue, handler, owner="analysis", task_kind="analyze"
        )
        assert await worker.run_once() is True

    detail = await client.get(resource_url)
    assert detail.status_code == HTTPStatus.OK
    assert_schema(detail, "ReviewDetail")
    result = detail.json()
    assert result["status"] == "COMPLETED"
    assert result["finished_at"] is not None
    assert result["summary"] == "Division by zero must be fixed."
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 40}
    assert result["publication_status"] == "PENDING"
    findings = await client.get(f"{resource_url}/findings")
    assert findings.status_code == HTTPStatus.OK
    assert_schema(findings, "FindingPage")
    assert findings.json()["next_cursor"] is None
    items = findings.json()["items"]
    assert len(items) == expected_count
    assert len({item["fingerprint"] for item in items}) == expected_count
    assert {item["title"] for item in items} == (
        {"Division by zero", "Distinct issue at the same location"}
        if duplicate_findings
        else {"Division by zero"}
    )
    finding = next(item for item in items if item["title"] == "Division by zero")
    assert finding["explanation"] == "This expression always raises."
    assert len(finding["fingerprint"]) == SHA256_HEX_LENGTH
    assert {
        key: finding[key]
        for key in (
            "path",
            "side",
            "start_line",
            "end_line",
            "category",
            "severity",
            "title",
            "publication_reference",
        )
    } == {
        "path": "src/a.py",
        "side": "NEW",
        "start_line": 1,
        "end_line": 1,
        "category": "correctness",
        "severity": "high",
        "title": "Division by zero",
        "publication_reference": None,
    }
    async with factory() as session:
        review = await session.get(models.ReviewJob, review_id)
        assert review is not None and review.finished_at is not None
        assert review.status == "COMPLETED"
        assert review.final_summary == result["summary"]
        analyze = await session.get(models.OutboxEvent, uuid.UUID(event_id))
        assert analyze is not None and analyze.broker_published_at is not None
        publish = (
            await session.scalars(
                select(models.OutboxEvent).where(
                    models.OutboxEvent.review_job_id == review_id,
                    models.OutboxEvent.task_kind == "publish",
                )
            )
        ).one()
        assert publish.broker_published_at is None
        stored = (
            await session.scalars(
                select(models.Finding)
                .join(models.ChunkResult)
                .where(models.ChunkResult.review_job_id == review_id)
            )
        ).all()
        assert len(stored) == expected_count
        assert {row.fingerprint for row in stored} == {
            item["fingerprint"] for item in items
        }
    pending = await redis_client.xpending(f"{stream_prefix}:analyze", "review-workers")
    assert pending["pending"] == 0
