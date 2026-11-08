"""Separate QA-04B paired Worker experiment with independent live authorization."""

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.redis_queue import RedisQueue
from app.adapters.review_admission import admit_review
from app.api.review_start_dependencies import get_review_start_authorizer
from app.application.review_start_ports import ReviewAdmissionRequest
from app.context.rules import parse_rules
from app.core.config import Auth, Github, Settings
from app.db import models
from app.domain.repository_policy import StoredReviewPolicy
from app.domain.settings import compute_rules_digest
from app.review.configuration import (
    load_runtime_configuration,
)
from app.review.prompt import PromptBuilder
from scripts.eval_harness.dataset import Fixture
from scripts.eval_harness.provenance import dataset_digest, digest
from scripts.eval_harness.rules_dataset import load_rules_dataset
from scripts.eval_harness.rules_report import build_rules_report, write_rules_report
from scripts.eval_harness.validator import is_grounded, parse_diff, validate_shape
from scripts.eval_harness.worker import (
    RECEIPT_FIELDS,
    CaseTransport,
    cleanup_campaign,
    load_sources,
    process_review,
    runtime_values,
    semantic_context,
    write_journal,
)

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / ".agents/reviewer"


class NeutralTransport(CaseTransport):
    """No fixture identity or labels in GitHub metadata sent to the model."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.observations: list[dict[str, Any]] = []

    async def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.eurouter.ai":
            validate_model_input(json.loads(request.content), self.fixture)
        response = await super().respond(request)
        if request.url.host == "api.eurouter.ai":
            await self.observe_response(response)
        return response

    async def observe_response(self, response: httpx.Response) -> None:
        """Keep public findings and safe diagnostics, never model input/headers."""
        await response.aread()
        try:
            result = json.loads(response.json()["choices"][0]["message"]["content"])
        except ValueError, KeyError, TypeError, IndexError:
            result = None
        async with self.factory() as session:
            attempt = await session.scalar(
                select(models.ReviewLlmAttempt)
                .where(
                    models.ReviewLlmAttempt.review_job_id == self.review_id,
                    models.ReviewLlmAttempt.state == "RESERVED",
                )
                .order_by(models.ReviewLlmAttempt.reserved_at.desc())
            )
            assert attempt is not None
            schema = json.loads(PromptBuilder.load(PROMPTS, "v4").output_schema_json)
            errors = validate_shape(result, schema)
            self.observations.append(
                {
                    "chunk_id": str(attempt.chunk_id),
                    "call_kind": attempt.call_kind,
                    "response": result,
                    "schema_errors": errors,
                    "grounding_errors": [
                        index
                        for index, finding in enumerate(result["findings"])
                        if not is_grounded(finding, parse_diff(self.fixture.diff_text))
                    ]
                    if not errors
                    else [],
                }
            )

    @property
    def name(self) -> str:
        return "sample-repository"

    @property
    def pull(self) -> dict[str, Any]:
        return super().pull | {"title": "Review proposed change", "body": ""}


async def run_rules_benchmark(  # noqa: PLR0912, PLR0913, PLR0915 -- bounded campaign lifecycle
    dataset: Path,
    *,
    live: bool = False,
    disposable: bool = False,
    approval: Path | None = None,
    journal: Path | None = None,
    provider_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Admit paired jobs and export the prepared production Worker evidence."""
    if live and approval is None:
        raise ValueError("New corpus requires explicit live approval")
    if not disposable:
        raise ValueError("Explicit disposable environment acknowledgement required")
    if live and journal is None:
        raise ValueError("Live approval requires a durable once-only journal")
    authorization = (
        validate_live_approval(approval, dataset, journal)
        if live and approval is not None and journal is not None
        else None
    )
    state: dict[str, Any] = {}
    if authorization is not None:
        state = {
            "state": "running",
            "approval_sha256": digest(authorization),
            "reserved_upper_bound_usd": authorization["reserved_upper_bound_usd"],
            "requests": 0,
            "jobs": [],
            "repositories": [],
            "actors": [],
        }
        assert journal is not None
        write_journal(journal, state, create=True)
    fixtures = load_rules_dataset(dataset)
    manifest, source_digest = load_sources(dataset / "sources", dataset)
    rules = (dataset / "rules.md").read_bytes()
    db_url, redis_url = disposable_urls()
    common_values = runtime_values()
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repositories: list[UUID] = []
    actors: list[UUID] = []
    rows: list[dict[str, Any]] = []
    current: NeutralTransport | None = None

    async def observe_request(request: httpx.Request) -> None:
        assert authorization is not None and current is not None and journal is not None
        body = json.loads(request.content)
        validate_model_input(body, current.fixture)
        if (
            str(request.url) != authorization["destination"]
            or request.method != "POST"
            or body.get("model") != authorization["model"]
        ):
            raise ValueError("Unapproved provider destination or model")
        if state["requests"] >= authorization["max_requests"]:
            raise ValueError("Live request count exhausted")
        await current.ensure_committed_debit()
        current.calls.append(body)
        state["requests"] += 1
        write_journal(journal, state)

    async def observe_response(response: httpx.Response) -> None:
        assert current is not None
        await current.observe_response(response)

    provider_http = (
        httpx.AsyncClient(
            transport=provider_transport,
            follow_redirects=False,
            trust_env=False,
            event_hooks={"request": [observe_request], "response": [observe_response]},
        )
        if live
        else None
    )
    complete = False
    try:
        with TemporaryDirectory() as directory:
            runtime_path = Path(directory) / "runtime.json"
            runtime_path.write_text(json.dumps(common_values))
            runtime = load_runtime_configuration(runtime_path)
            async with Redis.from_url(redis_url, decode_responses=True) as redis:
                prefix = "dmc268:qa04b:" + uuid4().hex
                queue = RedisQueue(redis, stream_prefix=prefix)
                try:
                    for fixture in fixtures:
                        actor, repository_id, settings_id = uuid4(), uuid4(), uuid4()
                        repositories.append(repository_id)
                        actors.append(actor)
                        if authorization is not None:
                            assert journal is not None
                            state["repositories"].append(str(repository_id))
                            state["actors"].append(str(actor))
                            write_journal(journal, state)
                        external_id = str(uuid4().int % (10**15) + 1)
                        key = Fernet.generate_key()
                        settings = Settings(  # type: ignore[call-arg]  # BaseSettings runtime option
                            _env_file=None,
                            github=Github(token=SecretStr("synthetic-bot")),
                            auth=Auth(credential_key=SecretStr(key.decode())),
                        )
                        name = "sample-repository"
                        async with factory.begin() as session:
                            session.add(models.User(id=actor))
                            session.add(
                                models.Repository(
                                    id=repository_id,
                                    external_id=external_id,
                                    provider_name="github",
                                    repo_name=name,
                                    owner="qa27",
                                )
                            )
                            await session.flush()
                            session.add(
                                models.GithubIdentity(
                                    user_id=actor,
                                    external_id=str(actor.int),
                                    login="qa27-admin",
                                    encrypted_token=Fernet(key)
                                    .encrypt(b"synthetic-personal")
                                    .decode(),
                                )
                            )
                            session.add(
                                models.RepositoryAccess(
                                    repository_id=repository_id,
                                    user_id=actor,
                                    role="admin",
                                )
                            )
                            session.add(
                                models.RepositorySettings(
                                    id=settings_id,
                                    repository_id=repository_id,
                                    rules={"instructions": []},
                                    ignores={"globs": []},
                                    rules_digest=compute_rules_digest(
                                        {"instructions": []}
                                    ),
                                    output_language="ru",
                                    surrounding_lines=5,
                                    max_starts_per_hour=10,
                                    max_active_jobs=4,
                                    review_policy=StoredReviewPolicy().model_dump(
                                        mode="json"
                                    ),
                                )
                            )
                            await session.flush()
                            repository = await session.get(
                                models.Repository, repository_id
                            )
                            assert repository is not None
                            repository.current_settings_id = settings_id
                        for pass_name in ("baseline", "rules"):
                            scenario = NeutralTransport(
                                fixture,
                                manifest["fixtures"][fixture.id],
                                dataset / "sources",
                                external_id,
                                factory,
                                rules=None if pass_name == "baseline" else rules,
                            )
                            current = scenario
                            with (
                                patch(
                                    "httpx._client.AsyncHTTPTransport",
                                    lambda scenario=scenario, **kwargs: (
                                        httpx.MockTransport(scenario.respond)
                                    ),
                                ),
                                patch(
                                    "app.adapters.review_admission.get_settings",
                                    lambda settings=settings: settings,
                                ),
                            ):
                                async with factory() as session:
                                    review_id, _, created = await admit_review(
                                        ReviewAdmissionRequest(
                                            actor,
                                            repository_id,
                                            1,
                                            scenario.head,
                                            None,
                                            str(uuid4()),
                                            "qa27",
                                        ),
                                        session=session,
                                        authorizer=get_review_start_authorizer(
                                            settings, session
                                        ),
                                        resolver=runtime.admission_resolver(
                                            factory, PROMPTS
                                        ),
                                    )
                                    assert created
                                scenario.review_id = review_id
                                if authorization is not None:
                                    assert journal is not None
                                    state["jobs"].append(
                                        {
                                            "fixture_id": fixture.id,
                                            "pass": pass_name,
                                            "review_id": str(review_id),
                                        }
                                    )
                                    write_journal(journal, state)
                                await process_review(
                                    factory,
                                    queue,
                                    runtime,
                                    httpx.MockTransport(scenario.respond),
                                    review_id=review_id,
                                    provider_http=provider_http,
                                )
                            row = await collect_row(factory, scenario, pass_name)
                            if live and any(
                                receipt["actual_input_tokens"] is None
                                or receipt["actual_output_tokens"] is None
                                for receipt in row["usage_receipts"]
                            ):
                                state["unknown_usage_receipts"] = row["usage_receipts"]
                                raise ValueError(
                                    "Uncertain provider usage; "
                                    "journal retained; replay refused"
                                )
                            validate_analysis_complete(row)
                            validate_frozen_identity(
                                row,
                                base=scenario.base,
                                head=scenario.head,
                                rules=scenario.rules,
                            )
                            validate_frozen_selection(
                                row["frozen_selection"], common_values["admission"]
                            )
                            rows.append(row)
                finally:
                    await redis.delete(prefix + ":analyze", prefix + ":publish")
        complete = True
    finally:
        try:
            if live:
                assert journal is not None
                state["state"] = "complete" if complete else "uncertain"
                write_journal(journal, state)
            else:
                await cleanup_campaign(factory, repositories, actors)
        finally:
            if provider_http is not None:
                await provider_http.aclose()
            await engine.dispose()
    receipts = [receipt for row in rows for receipt in row["usage_receipts"]]
    result: dict[str, Any] = {
        "rows": rows,
        "provenance": {
            "mode": "live" if live else "deterministic",
            "quality_measurement": bool(
                live
                and provider_transport is None
                and all(row["requests"] > 0 for row in rows)
            ),
            "provider_transport": "injected-test"
            if provider_transport is not None
            else "real-provider"
            if live
            else "scripted",
            "rules_sha256": hashlib.sha256(rules).hexdigest(),
            "dataset_sha256": dataset_digest(dataset),
            "source_bundle_sha256": source_digest,
            "model": common_values["admission"]["primary"]["model_ref"],
            "prompt_version": "v4",
            "prompt_digest": common_values["admission"]["prompt_digest"],
            "config_sha256": digest(common_values),
            "provider_schema_sha256": digest(
                json.loads(PromptBuilder.load(PROMPTS, "v4").output_schema_json)
            ),
            "canonical_schema_sha256": digest(
                json.loads((ROOT / "schemas/review-result/v2.json").read_text())
            ),
        },
        "usage": {
            "requests": sum(row["requests"] for row in rows),
            "cost_usd": "0",
            "input_tokens": sum(
                receipt["actual_input_tokens"] or 0 for receipt in receipts
            ),
            "output_tokens": sum(
                receipt["actual_output_tokens"] or 0 for receipt in receipts
            ),
            "unknown_attempts": sum(
                receipt["actual_input_tokens"] is None
                or receipt["actual_output_tokens"] is None
                for receipt in receipts
            ),
        },
    }
    if authorization is not None:
        receipts = [receipt for row in rows for receipt in row["usage_receipts"]]
        charged = {
            side: sum(
                receipt[f"actual_{side}_tokens"]
                if receipt[f"actual_{side}_tokens"] is not None
                else receipt[f"reserved_{side}_tokens"]
                for receipt in receipts
            )
            for side in ("input", "output")
        }
        result["provenance"]["live_authorization"] = {
            name: authorization[name]
            for name in (
                "reserved_upper_bound_usd",
                "max_requests",
                "max_cost_usd",
                "input_usd_per_million",
                "output_usd_per_million",
                "prices_verified_at",
                "price_evidence",
            )
        }
        result["usage"]["cost_usd"] = str(
            (
                charged["input"] * Decimal(authorization["input_usd_per_million"])
                + charged["output"] * Decimal(authorization["output_usd_per_million"])
            )
            / Decimal(1000000)
        )
        result["usage"].update(
            {f"charged_{side}_tokens": value for side, value in charged.items()}
        )
        state["usage"] = result["usage"]
        assert journal is not None
        write_journal(journal, state)
    return result


async def collect_row(
    factory: async_sessionmaker[AsyncSession],
    scenario: NeutralTransport,
    pass_name: str,
) -> dict[str, Any]:
    """Read the exact Worker's persisted canonical result and frozen identity."""
    async with factory() as session:
        job = await session.get(models.ReviewJob, scenario.review_id)
        assert job is not None
        effective = await session.get(
            models.RepositorySettings, job.repository_settings_id
        )
        assert effective is not None
        chunks = list(
            await session.scalars(
                select(models.ChunkResult)
                .where(models.ChunkResult.review_job_id == scenario.review_id)
                .order_by(models.ChunkResult.id)
            )
        )
        findings = (
            list(
                await session.scalars(
                    select(models.Finding)
                    .where(
                        models.Finding.chunk_result_id.in_(
                            [chunk.id for chunk in chunks]
                        )
                    )
                    .order_by(models.Finding.fingerprint)
                )
            )
            if chunks
            else []
        )
        contexts = list(
            await session.scalars(
                select(models.ContextPayload).where(
                    models.ContextPayload.review_job_id == scenario.review_id
                )
            )
        )
        attempts = list(
            await session.scalars(
                select(models.ReviewLlmAttempt)
                .where(models.ReviewLlmAttempt.review_job_id == scenario.review_id)
                .order_by(models.ReviewLlmAttempt.reserved_at)
            )
        )
        schema = json.loads((ROOT / "schemas/review-result/v2.json").read_text())
        canonical = (
            {
                "schema_version": chunks[0].schema_version,
                "summary": "\n\n".join(chunk.summary for chunk in chunks),
                "limitations": sorted(
                    {item for chunk in chunks for item in chunk.limitations}
                ),
                "usage": {
                    key: sum(
                        chunk.usage[key] for chunk in chunks if chunk.usage is not None
                    )
                    for key in ("input_tokens", "output_tokens")
                }
                if all(
                    chunk.usage is not None
                    and all(
                        chunk.usage[key] is not None
                        for key in ("input_tokens", "output_tokens")
                    )
                    for chunk in chunks
                )
                else None,
                "latency_ms": sum(chunk.latency_ms for chunk in chunks),
                "findings": [
                    {
                        name: getattr(finding, name)
                        for name in schema["$defs"]["finding"]["properties"]
                    }
                    for finding in findings
                ],
            }
            if chunks
            else None
        )
        context_digests = sorted(
            semantic_context(context.payload_body) for context in contexts
        )
        return {
            "fixture_id": scenario.fixture.id,
            "id": scenario.fixture.id,
            "pass": pass_name,
            "canonical_result": canonical,
            "worker_status": job.status,
            "chunk_count": len(chunks),
            "requests": len(scenario.calls),
            "source": {
                "base": scenario.base,
                "head": scenario.head,
                "transport_diff_sha256": hashlib.sha256(
                    scenario.transport_diff.encode()
                ).hexdigest(),
            },
            "context_snapshots": [
                context.payload_body["snapshot"] for context in contexts
            ],
            "rules_digest": effective.rules_digest,
            "rules_source": effective.rules_source,
            "frozen_policy": job.model_settings,
            "frozen_selection": {
                name: getattr(job, name)
                for name in (
                    "model_ref",
                    "model_digest",
                    "prompt_version",
                    "prompt_digest",
                )
            },
            "ast": {
                "semantic_sha256": context_digests[0]
                if len(context_digests) == 1
                else digest(context_digests)
                if context_digests
                else None
            },
            "usage_receipts": [
                {name: getattr(attempt, name) for name in RECEIPT_FIELDS}
                for attempt in attempts
            ],
            "provider_observations": scenario.observations,
            "provider_result": scenario.observations[-1]["response"]
            if scenario.observations
            else None,
            "provider_schema_errors": [
                error
                for observation in scenario.observations
                for error in observation["schema_errors"]
            ],
            "grounding_errors": [
                error
                for observation in scenario.observations
                for error in observation["grounding_errors"]
            ],
        }


def disposable_urls() -> tuple[str, str]:
    """Require explicit test DSNs and loopback hosts before any client exists."""
    values = tuple(
        os.environ.get(name, "")
        for name in ("DMC268_TEST_DATABASE_URL", "DMC268_TEST_REDIS_URL")
    )
    if any(
        urlsplit(value).hostname not in {"localhost", "127.0.0.1", "::1"}
        for value in values
    ):
        raise ValueError("Disposable services must use explicit localhost test URLs")
    return values[0], values[1]


def validate_frozen_identity(
    row: dict[str, Any], *, base: str, head: str, rules: bytes | None
) -> None:
    """Check the persisted effective rules against the exact supplied profile."""
    instructions = list(parse_rules(rules.decode()).instructions) if rules else []
    expected = compute_rules_digest({"instructions": instructions})
    if row["rules_digest"] != expected:
        raise ValueError("Frozen rules identity digest mismatch")
    if row.get("chunk_count", 0) > 1:
        raise ValueError("Experimental scorer requires a single chunk per job")
    source = row["rules_source"]["file"]
    if source["revision"] != base:
        raise ValueError("Frozen rules base revision mismatch")
    expected_content = hashlib.sha256(rules).hexdigest() if rules is not None else None
    expected_state = "valid" if rules is not None else "absent"
    if (
        source.get("state") != expected_state
        or source.get("content_digest") != expected_content
    ):
        raise ValueError("Frozen rules file state or content digest mismatch")
    if any(
        snapshot["base_sha"] != base
        or snapshot["head_sha"] != head
        or snapshot.get("merge_base_sha", base) != base
        for snapshot in row["context_snapshots"]
    ):
        raise ValueError("Frozen analysis code snapshot mismatch")


def validate_live_approval(
    approval: Path, dataset: Path, journal: Path
) -> dict[str, Any]:
    """New campaign authorization binds exact public content and provider routing."""
    document: dict[str, Any] = json.loads(approval.read_text())
    _, source_digest = load_sources(dataset / "sources", dataset)
    expected = {
        "allow_live": True,
        "dataset_sha256": dataset_digest(dataset),
        "source_bundle_sha256": source_digest,
        "rules_sha256": hashlib.sha256((dataset / "rules.md").read_bytes()).hexdigest(),
        "runtime_sha256": digest(runtime_values()),
        "model": runtime_values()["admission"]["primary"]["model_ref"],
        "destination": "https://api.eurouter.ai/api/v1/chat/completions",
        "journal_path": str(journal.resolve()),
    }
    if document.get("allow_live") is not True or any(
        document.get(name) != value for name, value in expected.items()
    ):
        raise ValueError("Live approval identity does not match this exact campaign")
    try:
        if any(
            not isinstance(document.get(name), str)
            for name in (
                "input_usd_per_million",
                "output_usd_per_million",
                "max_cost_usd",
            )
        ):
            raise ValueError
        prices = [
            Decimal(document[name])
            for name in (
                "input_usd_per_million",
                "output_usd_per_million",
                "max_cost_usd",
            )
        ]
        if any(not price.is_finite() or price <= 0 for price in prices):
            raise ValueError
    except ValueError, KeyError, TypeError, InvalidOperation:
        raise ValueError(
            "Live prices and cap must be positive finite decimals"
        ) from None
    limits = runtime_values()["admission"]["policy"]["limits"]
    worst = (
        (
            prices[0] * limits["run_max_input_tokens"]
            + prices[1] * limits["run_max_output_tokens"]
        )
        * 24
        / Decimal(1000000)
    )
    if worst > prices[2]:
        raise ValueError("Live cap below reserved upper bound for all 24 jobs")
    requests = document.get("max_requests")
    if type(requests) is not int or not 0 < requests <= 24 * limits["run_max_requests"]:
        raise ValueError("Live request count must be bounded positive integer")
    document["reserved_upper_bound_usd"] = str(worst)
    try:
        verified_at = datetime.fromisoformat(document["prices_verified_at"])
        if not timedelta(0) <= datetime.now(UTC) - verified_at <= timedelta(days=1):
            raise ValueError
        if (
            not isinstance(document.get("price_evidence"), str)
            or not document["price_evidence"].strip()
        ):
            raise ValueError
    except ValueError, TypeError, KeyError:
        raise ValueError(
            "Live price evidence must be nonempty and verified within 24 hours"
        ) from None
    return document


def validate_model_input(body: dict[str, Any], fixture: Fixture) -> None:
    """Check decoded provider payload, including metadata, before dispatch."""
    pending: list[Any] = [body]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and any(
            label and label in value
            for label in (fixture.id, fixture.title, fixture.description)
        ):
            raise ValueError("Ground-truth fixture metadata leaked into model input")
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def validate_frozen_selection(
    selection: dict[str, Any], admission: dict[str, Any]
) -> None:
    """Read back model/prompt identity from the admitted job, not report defaults."""
    expected = {
        "model_ref": admission["primary"]["model_ref"],
        "model_digest": admission["primary"]["model_digest"],
        "prompt_version": admission["prompt_version"],
        "prompt_digest": admission["prompt_digest"],
    }
    if selection != expected:
        raise ValueError("Frozen model or prompt differs from controlled runtime")


def validate_analysis_complete(row: dict[str, Any]) -> None:
    """Deferred work remains incomplete even when a provider already responded."""
    if row["worker_status"] not in {"COMPLETED", "PARTIAL", "FAILED", "SKIPPED"}:
        raise ValueError("Campaign contains incomplete Worker analysis")


def run(arguments: list[str] | None = None) -> dict[str, Any]:
    """Reproduce the separate paired campaign; live requires its own approval."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("deterministic", "live"), default="deterministic"
    )
    parser.add_argument(
        "--dataset-dir", type=Path, default=ROOT / "tests/fixtures/rules-benchmark"
    )
    parser.add_argument("--disposable", action="store_true")
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument(
        "--output-dir", type=Path, default=Path(".scratch/rules-quality")
    )
    args = parser.parse_args(arguments)
    execution = asyncio.run(
        run_rules_benchmark(
            args.dataset_dir,
            live=args.mode == "live",
            disposable=args.disposable,
            approval=args.approval,
            journal=args.journal,
        )
    )
    report = build_rules_report(args.dataset_dir, execution)
    write_rules_report(report, args.output_dir)
    return report


if __name__ == "__main__":
    run()
