"""Stage 8A: real Gateway, HTTP mocks and the authoritative PostgreSQL seam.

Composition is deliberately test-only: these fixtures attest a known frozen
configuration. Production configuration resolution/digest policy is separate.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_analysis_handler import github_http
from test_contracts import SCHEMAS, _validator
from test_postgres_integration import PostgresHarness

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.application.ports import DurableTaskOutcome, QueueTaskEnvelope
from app.core.config import LLM, Eurorouter
from app.db.models import (
    ChunkResult,
    ContextPayload,
    Finding,
    OutboxEvent,
    RepositorySettings,
    ReviewEvent,
    ReviewJob,
)
from app.db.task_leases import PostgresTaskLeaseStore
from app.db.worker_tasks import PostgresWorkerTasks
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.providers import build_gateway
from app.llm.tokens import ConservativeTokenCounter
from app.review.port_adapter import LlmGatewayAdapter
from app.review.prompt import PromptBuilder
from app.review.service import ReviewService
from app.review.validation import FindingValidator
from app.vcs.github import GithubClient

pytest_plugins = ("test_analysis_handler",)
PROMPTS = Path(__file__).resolve().parents[1] / ".agents/reviewer"
OUTPUT = {
    "summary": "Division by zero must be fixed.",
    "limitations": [],
    "findings": [
        {
            "path": "src/a.py",
            "side": "NEW",
            "start_line": 1,
            "end_line": 1,
            "category": "correctness",
            "severity": "high",
            "title": "Division by zero",
            "explanation": "This expression always raises.",
            "evidence": "return value / 0",
            "recommendation": "Remove the division.",
            "proposed_diff_fix": None,
        }
    ],
}


async def frozen_adapter(
    factory: async_sessionmaker[AsyncSession],
    review: uuid.UUID,
    http: httpx.AsyncClient,
    *,
    limits: dict[str, int] | None = None,
) -> tuple[LlmGatewayAdapter, LLM]:
    """Attest the fixture, load its stored version/model; never read environment.

    The opaque fixture prompt_digest is an explicit test attestation, not a new
    production hashing convention. PromptBuilder verifies *all* manifest files.
    Unsupported settings/rules must not be silently ignored by this composition.
    """
    async with factory() as session:
        job = await session.get(ReviewJob, review)
        assert job is not None
        assert job.model_digest is None
        assert job.model_settings["generation"] == {"temperature": 0.0}
        assert job.prompt_digest == "prompt-digest"
        repository_settings = await session.get(
            RepositorySettings, job.repository_settings_id
        )
        assert repository_settings is not None
        assert repository_settings.rules == {"instructions": []}
        settings = LLM(
            fallback_enabled=False,
            eurorouter=Eurorouter(
                base_url="https://llm.invalid/v1",
                model=job.model_ref,
                api_keys=[SecretStr("synthetic-test-key")],
            ),
            prompt_version=job.prompt_version,
        )
        settings = LLM.model_validate({**settings.model_dump(), **(limits or {})})
    counter = ConservativeTokenCounter()
    prompts = PromptBuilder.load(PROMPTS, settings.prompt_version, counter)
    service = ReviewService(
        build_gateway(settings, http, sleep=no_sleep),
        prompts,
        FindingValidator(),
        counter,
        settings,
    )
    return LlmGatewayAdapter(
        service, RunBudget(BudgetLimits.from_settings(settings))
    ), settings


async def no_sleep(delay: float) -> None:
    """Retry timing is outside this integration seam."""


async def analysis_task(
    factory: async_sessionmaker[AsyncSession],
    review: uuid.UUID,
) -> QueueTaskEnvelope:
    async with factory() as session:
        event = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review,
                    OutboxEvent.task_kind == "analyze",
                )
            )
        ).one()
        return QueueTaskEnvelope(
            1,
            str(event.id),
            str(review),
            "analyze",
            event.attempt,
            event.trace_id,
        )


async def test_real_gateway_persists_analysis_with_token_estimate(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM__EUROROUTER__MODEL", "different-current-model")
    monkeypatch.setenv("LLM__PROMPT_VERSION", "unavailable-current-version")
    factory = async_sessionmaker(postgres.engine)
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "llm.invalid"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(OUTPUT)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    task = await analysis_task(factory, review_id)
    leases = PostgresTaskLeaseStore(factory)
    lease = await leases.acquire(review_id, owner="gateway-test", task_kind="analyze")
    assert lease is not None
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
    ):
        adapter, settings = await frozen_adapter(factory, review_id, http)
        handler = PostgresAnalysisHandler(
            factory,
            GithubClient(github),
            adapter,
            schemas_dir=SCHEMAS,
            limit_input_tokens=settings.max_input_tokens,
            reserved_output_tokens=settings.max_output_tokens,
        )
        assert await handler.handle(task, lease) is DurableTaskOutcome.COMPLETED
        assert await PostgresWorkerTasks(factory).completed(task, lease)
    async with factory() as session:
        job = await session.get(ReviewJob, review_id)
        context = (
            await session.scalars(
                select(ContextPayload).where(
                    ContextPayload.review_job_id == review_id,
                )
            )
        ).one()
        chunk = (
            await session.scalars(
                select(ChunkResult).where(
                    ChunkResult.review_job_id == review_id,
                )
            )
        ).one()
        findings = (
            await session.scalars(
                select(Finding).where(
                    Finding.chunk_result_id == chunk.id,
                )
            )
        ).all()
        publish = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).one()
    assert job is not None and job.status == "COMPLETED"
    assert job.final_summary == chunk.summary == OUTPUT["summary"]
    assert len(findings) == 1 and findings[0].evidence == "return value / 0"
    assert chunk.context_payload_id == context.id
    assert chunk.usage == {"input_tokens": 100, "output_tokens": 40}
    assert publish.trace_id == task.trace_id
    assert len(requests) == 1 and requests[0]["model"] == job.model_ref == "test-model"
    assert requests[0]["temperature"] == 0
    assert requests[0]["response_format"]["type"] == "json_schema"
    wire = {
        "schema_version": 1,
        "review_id": str(review_id),
        "chunk_id": str(context.id),
        **context.payload_body,
    }
    _validator(SCHEMAS / "context/v1.json").validate(wire)
    # The complete wire estimate includes IDs and its own decimal width. A token
    # estimate must be smaller than its UTF-8 byte count for this fixture.
    encoded = json.dumps(wire, ensure_ascii=False)
    assert wire["budget"]["estimated_input_tokens"] == ConservativeTokenCounter().count(
        encoded
    )
    assert wire["budget"]["estimated_input_tokens"] < len(encoded.encode("utf-8"))
    await leases.release(lease)


@pytest.mark.parametrize(
    "case",
    [
        (503, {}, "llm_unavailable", 2),
        (401, {}, "llm_unavailable", 1),
        (400, {}, "provider_request_error", 1),
        (400, {"max_input_tokens": 1}, "prompt_too_large", 0),
        (503, {"run_max_requests": 1}, "budget_exceeded", 1),
        (400, {}, "context_overflow", 2),
    ],
)
async def test_gateway_failure_finishes_without_restarting_provider_budget(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    case: tuple[int, dict[str, int], str, int],
) -> None:
    http_status, limits, error_code, expected_requests = case
    factory = async_sessionmaker(postgres.engine)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            http_status,
            json={
                "error": {
                    "code": "context_length_exceeded"
                    if error_code == "context_overflow"
                    else "private",
                    "message": "private provider data",
                }
            },
        )

    task = await analysis_task(factory, review_id)
    leases = PostgresTaskLeaseStore(factory)
    lease = await leases.acquire(review_id, owner="gateway-test", task_kind="analyze")
    assert lease is not None
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
    ):
        adapter, _ = await frozen_adapter(factory, review_id, http, limits=limits)
        handler = PostgresAnalysisHandler(
            factory,
            GithubClient(github),
            adapter,
            schemas_dir=SCHEMAS,
        )
        if http_status == 503 and not limits:  # noqa: PLR2004 -- transient case
            assert await handler.handle(task, lease) is None
            assert not await PostgresWorkerTasks(factory).completed(task, lease)
            async with factory() as session:
                job = await session.get(ReviewJob, review_id)
                assert job is not None and job.retry_at is not None
                event = (
                    await session.scalars(
                        select(ReviewEvent).where(
                            ReviewEvent.review_job_id == review_id,
                            ReviewEvent.event_type == "FAILURE",
                        )
                    )
                ).one()
                assert event.retryable
                assert event.safe_details == {"error_code": "provider_unavailable"}
            assert len(requests) == expected_requests
            await leases.release(lease)
            return
        assert await handler.handle(task, lease) is DurableTaskOutcome.COMPLETED
        # Terminal durable decision prevents a worker from invoking LLM again
        # on redelivery; its existing completed() gate is part of that contract.
        assert await PostgresWorkerTasks(factory).completed(task, lease)
    async with factory() as session:
        job = await session.get(ReviewJob, review_id)
        failed = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.status == "FAILED",
                )
            )
        ).one()
        assert (
            await session.scalar(
                select(ChunkResult.id).where(
                    ChunkResult.review_job_id == review_id,
                )
            )
            is None
        )
        assert (
            await session.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
            is None
        )
    assert job is not None and job.status == "FAILED" and job.finished_at is not None
    assert failed.reason_code == "LLM_UNAVAILABLE" and failed.retryable is False
    assert failed.safe_details == {
        "error_code": "provider_auth_failed" if http_status == 401 else error_code  # noqa: PLR2004 -- auth case
    }
    assert len(requests) == expected_requests
    await leases.release(lease)


async def test_invalid_gateway_output_is_terminal_and_keeps_safe_error_code(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(postgres.engine)
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "private invalid output"}}],
            },
        )

    task = await analysis_task(factory, review_id)
    leases = PostgresTaskLeaseStore(factory)
    lease = await leases.acquire(review_id, owner="gateway-test", task_kind="analyze")
    assert lease is not None
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
    ):
        adapter, settings = await frozen_adapter(factory, review_id, http)
        handler = PostgresAnalysisHandler(
            factory,
            GithubClient(github),
            adapter,
            schemas_dir=SCHEMAS,
            limit_input_tokens=settings.max_input_tokens,
            reserved_output_tokens=settings.max_output_tokens,
        )
        assert await handler.handle(task, lease) is DurableTaskOutcome.COMPLETED
        assert await PostgresWorkerTasks(factory).completed(task, lease)
    async with factory() as session:
        job = await session.get(ReviewJob, review_id)
        chunks = (
            await session.scalars(
                select(ChunkResult).where(
                    ChunkResult.review_job_id == review_id,
                )
            )
        ).all()
        failed = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.status == "FAILED",
                )
            )
        ).one()
        publish = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).all()
    assert job is not None and job.status == "FAILED" and job.finished_at is not None
    assert failed.reason_code == "LLM_OUTPUT_INVALID"
    assert failed.retryable is False
    assert failed.safe_details == {"error_code": "invalid_llm_output"}
    assert chunks == [] and publish == [] and job.final_summary is None
    assert len(requests) == 2  # noqa: PLR2004 — initial request + Gateway repair
    repair = requests[1]["messages"]
    assert repair[-2] == {"role": "assistant", "content": "private invalid output"}
    assert "JSON Schema" in repair[-1]["content"]
    await leases.release(lease)
