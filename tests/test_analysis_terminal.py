"""AC25: terminal outcomes through handler, PostgreSQL and REST projections."""

# ruff: noqa: PLR2004 -- literal HTTP and request-count expectations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_analysis_accounting import freeze_test_policy
from test_analysis_gateway import OUTPUT, analysis_task
from test_analysis_handler import DeterministicLlm, github_http
from test_frozen_configuration import runtime_profiles
from test_postgres_integration import PostgresHarness

from app.adapters.analysis_handler import PostgresAnalysisHandler
from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.db.llm_accounting import PostgresReviewRunBudget
from app.db.models import (
    ChunkResult,
    ContextPayload,
    Finding,
    OutboxEvent,
    Publication,
    RepositoryAccess,
    RepositorySettings,
    ReviewEvent,
    ReviewJob,
    ReviewLlmAttempt,
    User,
)
from app.db.session import get_session
from app.db.task_leases import PostgresTaskLeaseStore
from app.domain.settings import compute_repository_settings_digest
from app.llm.tokens import ConservativeTokenCounter
from app.main import create_app
from app.review.configuration import (
    FrozenConfigurationReader,
    RuntimeProfile,
    build_frozen_gateway,
)
from app.review.port_adapter import LlmGatewayAdapter
from app.review.prompt import PromptBuilder
from app.review.service import ReviewService
from app.review.validation import FindingValidator
from app.vcs.github import GithubClient

pytest_plugins = ("test_analysis_handler",)
PROMPTS = Path(".agents/reviewer")
PROFILES = {
    "eurorouter/review-v1": RuntimeProfile(
        "eurorouter", "https://llm.invalid/v1", ("test-key",), ("test-model",)
    )
}


@pytest_asyncio.fixture(autouse=True)
async def ledger_cleanup(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> AsyncIterator[None]:
    yield
    async with async_sessionmaker(postgres.engine).begin() as session:
        await session.execute(
            delete(ReviewLlmAttempt).where(ReviewLlmAttempt.review_job_id == review_id)
        )


async def freeze(
    factory: async_sessionmaker[AsyncSession], review: uuid.UUID, **limits: int
) -> None:
    async with factory.begin() as session:
        job = await session.get(ReviewJob, review)
        assert job is not None
        settings = await session.get(RepositorySettings, job.repository_settings_id)
        assert settings is not None
        policy = dict(job.model_settings)
        policy["repository_settings_digest"] = compute_repository_settings_digest(
            id=settings.id,
            repository_id=settings.repository_id,
            rules=settings.rules,
            ignores=settings.ignores,
            output_language=settings.output_language,
            surrounding_lines=settings.surrounding_lines,
        )
        policy["limits"] = {**policy["limits"], **limits}
        digest = PromptBuilder.load(PROMPTS, job.prompt_version).prompt_digest
        assert digest is not None
        job.prompt_digest = digest
        freeze_test_policy(job, policy)


async def detail(
    factory: async_sessionmaker[AsyncSession], review: uuid.UUID
) -> dict[str, Any]:
    app = create_app()
    user = uuid.uuid4()
    async with factory.begin() as session:
        job = await session.get(ReviewJob, review)
        assert job is not None
        session.add(User(id=user))
        await session.flush()
        session.add(
            RepositoryAccess(
                repository_id=job.repository_id, user_id=user, role="reviewer"
            )
        )

    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_authenticated_principal] = lambda: (
        AuthenticatedPrincipal(user)
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get(f"/api/v1/reviews/{review}")
            assert response.status_code == 200
            return cast(dict[str, Any], response.json())
    finally:
        async with factory.begin() as session:
            await session.execute(
                delete(RepositoryAccess).where(RepositoryAccess.user_id == user)
            )
            await session.execute(delete(User).where(User.id == user))


async def manual_handler(  # noqa: PLR0913 -- explicit execution dependencies
    factory: Any,
    task: Any,
    lease: Any,
    github: httpx.AsyncClient,
    http: httpx.AsyncClient,
    *,
    attempt: int = 1,
) -> PostgresAnalysisHandler:
    config = await FrozenConfigurationReader(factory, PROMPTS, PROFILES).resolve(
        lease.review_job_id
    )
    service = ReviewService(
        build_frozen_gateway(config, PROFILES, http),
        PromptBuilder.load(
            PROMPTS, config.prompt_version, expected_digest=config.prompt_digest
        ),
        FindingValidator(),
        ConservativeTokenCounter(),
        config.service_settings,
        frozen_configuration=config,
    )
    return PostgresAnalysisHandler(
        factory,
        GithubClient(github),
        LlmGatewayAdapter(
            service,
            PostgresReviewRunBudget(
                factory, task=task, lease=lease, analysis_attempt=attempt
            ),
        ),
        schemas_dir=Path("schemas"),
        limit_input_tokens=10000,
        reserved_output_tokens=2000,
    )


@pytest.mark.parametrize(
    "dimension,limit,code",
    [
        ("requests", 1, "budget_requests_exhausted"),
        ("input_tokens", 1, "budget_input_exhausted"),
        ("output_tokens", 1, "budget_output_exhausted"),
    ],
)
async def test_budget_terminal_preserves_precise_safe_code(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    dimension: str,
    limit: int,
    code: str,
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id, **{f"run_max_{dimension}": limit})
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="terminal", task_kind="analyze"
    )
    assert lease is not None
    calls = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http,
    ):
        handler = await manual_handler(factory, task, lease, github, http)
        await handler.handle(task, lease)
    view = await detail(factory, review_id)
    assert view["status"] == "FAILED" and view["finished_at"] is not None
    assert view["error"]["reason_code"] == "LLM_UNAVAILABLE"
    async with factory() as session:
        event = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.status == "FAILED",
                )
            )
        ).one()
        assert event.safe_details == {"error_code": code}
    assert len(calls) == (1 if dimension == "requests" else 0)


@pytest.mark.parametrize(
    "retained_context", [True, False], ids=["context", "retention"]
)
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"input_tokens": 1, "output_tokens": 2},
        {"input_tokens": 0, "output_tokens": 0},
    ],
    ids=["null", "valid", "zero"],
)
@pytest.mark.parametrize("schema_version", [1, 2], ids=["v1", "v2"])
async def test_queue_deadline_preserves_validated_saved_result_as_partial(  # noqa: PLR0913, PLR0917 -- version/retention/usage matrix
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    payload_data: dict[str, Any],
    usage: dict[str, int] | None,
    retained_context: bool,
    schema_version: int,
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    async with factory.begin() as session:
        context = ContextPayload(
            id=uuid.uuid4(),
            review_job_id=review_id,
            schema_version=1,
            payload_body={
                key: value
                for key, value in payload_data.items()
                if key not in {"schema_version", "review_id", "chunk_id"}
            },
        )
        session.add(context)
        await session.flush()
        session.add(
            ChunkResult(
                id=uuid.uuid4(),
                review_job_id=review_id,
                context_payload_id=context.id,
                schema_version=schema_version,
                summary="Verified saved review",
                limitations=[],
                usage=usage,
                latency_ms=3,
            )
        )
        await session.flush()
        chunk = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).one()
        session.add(
            Finding(
                id=uuid.uuid4(),
                chunk_result_id=chunk.id,
                fingerprint="historical-fingerprint-kept-verbatim",
                path=payload_data["files"][0]["diff"]["new_path"],
                side="NEW",
                start_line=39,
                end_line=39,
                category="correctness",
                severity="high",
                title="Saved issue",
                explanation="Saved explanation",
                evidence="Saved evidence",
                recommendation="Saved recommendation",
                proposed_diff_fix=None,
                rule_ref="SEC-1" if schema_version == 2 else None,
            )
        )
        await session.flush()
        if not retained_context:
            await session.execute(
                delete(ContextPayload).where(ContextPayload.id == context.id)
            )
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(queue_deadline_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="partial", task_kind="analyze"
    )
    assert lease is not None
    async with httpx.AsyncClient(transport=github_http()) as github:
        llm = DeterministicLlm()
        await PostgresAnalysisHandler(
            factory, GithubClient(github), llm, schemas_dir=Path("schemas")
        ).handle(task, lease)
        assert llm.payloads == []
    view = await detail(factory, review_id)
    assert view["status"] == "PARTIAL"
    assert view["summary"] == "Verified saved review"
    assert view["error"]["reason_code"] == "QUEUE_DEADLINE_EXCEEDED"
    assert view["publication_status"] == "PENDING" and view["finished_at"] is not None
    async with factory() as session:
        assert (
            len(
                (
                    await session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.review_job_id == review_id,
                            OutboxEvent.task_kind == "publish",
                        )
                    )
                ).all()
            )
            == 1
        )
        chunk = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).one()
        assert "queue_deadline_exceeded" in chunk.limitations
        assert chunk.usage == usage
        assert chunk.schema_version == schema_version
        finding = (
            await session.scalars(
                select(Finding).where(Finding.chunk_result_id == chunk.id)
            )
        ).one()
        assert finding.fingerprint == "historical-fingerprint-kept-verbatim"
        assert finding.rule_ref == ("SEC-1" if schema_version == 2 else None)
        assert chunk.context_payload_id == (context.id if retained_context else None)
        publication = (
            await session.scalars(
                select(Publication).where(Publication.review_job_id == review_id)
            )
        ).one()
        assert publication.finished_at is None


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"input_tokens": -1, "output_tokens": 2}, id="negative-input"),
        pytest.param({"input_tokens": 1, "output_tokens": -2}, id="negative-output"),
        pytest.param({}, id="missing-both"),
        pytest.param({"output_tokens": 2}, id="missing-input"),
        pytest.param({"input_tokens": 1}, id="missing-output"),
        pytest.param({"input_tokens": "1", "output_tokens": 2}, id="string-input"),
        pytest.param({"input_tokens": 1, "output_tokens": False}, id="boolean-output"),
        pytest.param({"input_tokens": None, "output_tokens": 2}, id="null-input"),
        pytest.param(
            {"input_tokens": 1, "output_tokens": 2, "extra": 3}, id="extra-field"
        ),
        pytest.param([], id="array"),
        pytest.param("invalid", id="scalar"),
    ],
)
async def test_queue_deadline_rejects_saved_result_with_noncanonical_usage(
    postgres: PostgresHarness, review_id: uuid.UUID, usage: Any
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    async with factory.begin() as session:
        session.add(
            ChunkResult(
                id=uuid.uuid4(),
                review_job_id=review_id,
                schema_version=1,
                summary="Historical result with invalid usage",
                limitations=[],
                usage=usage,
                latency_ms=3,
            )
        )
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(queue_deadline_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="saved-usage", task_kind="analyze"
    )
    assert lease is not None
    async with httpx.AsyncClient(transport=github_http()) as github:
        await PostgresAnalysisHandler(
            factory,
            GithubClient(github),
            DeterministicLlm(),
            schemas_dir=Path("schemas"),
        ).handle(task, lease)
    view = await detail(factory, review_id)
    assert view["status"] == "FAILED"
    assert view["publication_status"] == "NOT_READY"
    assert view["finished_at"] is not None
    assert view["error"]["reason_code"] == "QUEUE_DEADLINE_EXCEEDED"
    async with factory() as session:
        chunk = (
            await session.scalars(
                select(ChunkResult).where(ChunkResult.review_job_id == review_id)
            )
        ).one()
        assert chunk.usage == usage
        publication = (
            await session.scalars(
                select(Publication).where(Publication.review_job_id == review_id)
            )
        ).one()
        assert publication.status == "NOT_READY"
        assert not (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).all()


async def test_deadline_during_provider_backoff_has_deadline_reason(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="deadline", task_kind="analyze"
    )
    assert lease is not None
    calls = []

    async def expire(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        async with factory.begin() as session:
            await session.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review_id)
                .values(
                    analysis_deadline_at=func.clock_timestamp() - timedelta(seconds=1)
                )
            )
        return httpx.Response(503)

    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(expire)) as http,
    ):
        handler = await manual_handler(factory, task, lease, github, http)
        await handler.handle(task, lease)
    view = await detail(factory, review_id)
    assert view["status"] == "FAILED"
    assert view["error"]["reason_code"] == "ANALYSIS_DEADLINE_EXCEEDED"
    async with factory() as session:
        event = (
            await session.scalars(
                select(ReviewEvent).where(
                    ReviewEvent.review_job_id == review_id,
                    ReviewEvent.status == "FAILED",
                )
            )
        ).one()
        assert event.safe_details == {"error_code": "analysis_deadline_exceeded"}
    assert len(calls) == 1


async def test_successful_fallback_without_result_limitations_is_completed(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    async with factory.begin() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None
        policy = dict(job.model_settings)
        policy["routing"] = {
            "primary_profile_ref": "eurorouter/review-v1",
            "fallback": [
                {
                    "profile_ref": "ollama/review-v1",
                    "model_ref": "test-model",
                    "model_digest": None,
                }
            ],
        }
        freeze_test_policy(job, policy)
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="fallback", task_kind="analyze"
    )
    assert lease is not None
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "llm.invalid":
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "message": {"content": json.dumps(OUTPUT)},
                "prompt_eval_count": 100,
                "eval_count": 40,
            },
        )

    profiles = runtime_profiles()
    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
    ):
        config = await FrozenConfigurationReader(factory, PROMPTS, profiles).resolve(
            review_id
        )
        service = ReviewService(
            build_frozen_gateway(config, profiles, http),
            PromptBuilder.load(
                PROMPTS, config.prompt_version, expected_digest=config.prompt_digest
            ),
            FindingValidator(),
            ConservativeTokenCounter(),
            config.service_settings,
            frozen_configuration=config,
        )
        handler = PostgresAnalysisHandler(
            factory,
            GithubClient(github),
            LlmGatewayAdapter(
                service,
                PostgresReviewRunBudget(
                    factory, task=task, lease=lease, analysis_attempt=1
                ),
            ),
            schemas_dir=Path("schemas"),
            limit_input_tokens=10000,
            reserved_output_tokens=2000,
        )
        await handler.handle(task, lease)
    view = await detail(factory, review_id)
    assert view["status"] == "COMPLETED"
    assert view["publication_status"] == "PENDING"
    assert len(calls) == 2


@pytest.mark.parametrize("saved", ["none", "valid", "invalid"])
@pytest.mark.parametrize(
    "failure", ["queue", "analysis", "attempts", "auth", "schema", "budget"]
)
async def test_terminal_failure_matrix_preserves_only_canonical_saved_results(
    postgres: PostgresHarness, review_id: uuid.UUID, saved: str, failure: str
) -> None:

    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(
        factory, review_id, **({"run_max_requests": 1} if failure == "budget" else {})
    )
    reason = {
        "queue": "QUEUE_DEADLINE_EXCEEDED",
        "analysis": "ANALYSIS_DEADLINE_EXCEEDED",
        "schema": "LLM_OUTPUT_INVALID",
    }.get(failure, "LLM_UNAVAILABLE")
    async with factory.begin() as session:
        if saved != "none":
            session.add(
                ChunkResult(
                    id=uuid.uuid4(),
                    review_job_id=review_id,
                    schema_version=1 if saved == "valid" else 99,
                    summary="Verified saved review"
                    if saved == "valid"
                    else "raw invalid answer",
                    limitations=[],
                    usage=None,
                    latency_ms=3,
                )
            )
        values: dict[str, Any] = {}
        if failure == "queue":
            values["queue_deadline_at"] = func.clock_timestamp() - timedelta(seconds=1)
        elif failure == "analysis":
            values["started_at"] = func.clock_timestamp() - timedelta(minutes=11)
            values["analysis_deadline_at"] = func.clock_timestamp() - timedelta(
                seconds=1
            )
        elif failure == "attempts":
            values.update(analysis_attempts=3, analysis_attempt_fence=55)
        if values:
            await session.execute(
                update(ReviewJob).where(ReviewJob.id == review_id).values(**values)
            )
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="matrix", task_kind="analyze"
    )
    assert lease is not None
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return (
            httpx.Response(401 if failure == "auth" else 503)
            if failure != "schema"
            else httpx.Response(
                200,
                json={
                    "model": "test-model",
                    "choices": [{"message": {"content": "invalid private output"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40},
                },
            )
        )

    async with (
        httpx.AsyncClient(transport=github_http()) as github,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
    ):
        handler = await manual_handler(factory, task, lease, github, http)
        await handler.handle(task, lease)
    view = await detail(factory, review_id)
    assert view["status"] == ("PARTIAL" if saved == "valid" else "FAILED")
    assert view["finished_at"] is not None and view["error"]["reason_code"] == reason
    assert view["publication_status"] == (
        "PENDING" if saved == "valid" else "NOT_READY"
    )
    async with factory() as session:
        events = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.review_job_id == review_id,
                    OutboxEvent.task_kind == "publish",
                )
            )
        ).all()
        assert len(events) == (1 if saved == "valid" else 0)
    assert (
        len(calls)
        == {
            "queue": 0,
            "analysis": 0,
            "attempts": 0,
            "auth": 1,
            "schema": 2,
            "budget": 1,
        }[failure]
    )


async def test_malformed_saved_context_does_not_abort_safe_terminalization(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    async with factory.begin() as session:
        context = ContextPayload(
            id=uuid.uuid4(),
            review_job_id=review_id,
            schema_version=1,
            payload_body={"files": [{}]},
        )
        session.add(context)
        await session.flush()
        session.add(
            ChunkResult(
                id=uuid.uuid4(),
                review_job_id=review_id,
                context_payload_id=context.id,
                schema_version=1,
                summary="Unverified historical result",
                limitations=[],
                latency_ms=1,
            )
        )
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(queue_deadline_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="malformed", task_kind="analyze"
    )
    assert lease is not None
    async with httpx.AsyncClient(transport=github_http()) as github:
        await PostgresAnalysisHandler(
            factory,
            GithubClient(github),
            DeterministicLlm(),
            schemas_dir=Path("schemas"),
        ).handle(task, lease)
    view = await detail(factory, review_id)
    assert view["status"] == "FAILED" and view["finished_at"] is not None
    assert view["publication_status"] == "NOT_READY"


async def test_inconsistent_saved_hunks_are_not_usable_for_partial(
    postgres: PostgresHarness, review_id: uuid.UUID, payload_data: dict[str, Any]
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    async with factory.begin() as session:
        job = await session.get(ReviewJob, review_id)
        assert job is not None
        body = {
            key: value
            for key, value in payload_data.items()
            if key not in {"schema_version", "review_id", "chunk_id"}
        }
        body["metadata"]["rules_version"] = str(job.repository_settings_id)
        body["files"][0]["diff"]["hunks"][0]["new_count"] = 3
        context = ContextPayload(
            id=uuid.uuid4(),
            review_job_id=review_id,
            schema_version=1,
            payload_body=body,
        )
        session.add(context)
        await session.flush()
        session.add(
            ChunkResult(
                id=uuid.uuid4(),
                review_job_id=review_id,
                context_payload_id=context.id,
                schema_version=1,
                summary="Historical unverified coordinates",
                limitations=[],
                latency_ms=1,
            )
        )
        await session.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(queue_deadline_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    task = await analysis_task(factory, review_id)
    lease = await PostgresTaskLeaseStore(factory).acquire(
        review_id, owner="invalid-hunks", task_kind="analyze"
    )
    assert lease is not None
    async with httpx.AsyncClient(transport=github_http()) as github:
        await PostgresAnalysisHandler(
            factory,
            GithubClient(github),
            DeterministicLlm(),
            schemas_dir=Path("schemas"),
        ).handle(task, lease)
    view = await detail(factory, review_id)
    assert view["status"] == "FAILED" and view["publication_status"] == "NOT_READY"
