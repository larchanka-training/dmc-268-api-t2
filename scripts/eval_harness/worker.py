"""Scripted QA driver over existing admission/queue/worker, never a model eval."""

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.analysis_composition import prepare_postgres_analysis_worker
from app.adapters.outbox_dispatcher import PostgresOutboxDispatcher
from app.adapters.redis_queue import RedisQueue
from app.adapters.review_admission import admit_review
from app.api.review_start_dependencies import get_review_start_authorizer
from app.application.review_start_ports import ReviewAdmissionRequest
from app.core.config import Auth, Github, Settings
from app.db import models
from app.domain.repository_policy import StoredReviewPolicy
from app.domain.settings import compute_rules_digest
from app.review.configuration import (
    TrustedRuntimeConfiguration,
    load_runtime_configuration,
)
from app.review.prompt import PromptBuilder, ReflectionBundle
from app.vcs.github import GithubClient
from scripts.eval_harness.dataset import Fixture, dataset_file
from scripts.eval_harness.llm_client import LlmConfig, LlmConfigurationError
from scripts.eval_harness.provenance import MATCHING_POLICY, dataset_digest, digest
from scripts.eval_harness.report import build_report, evaluate_fixture
from scripts.eval_harness.runtime import MODEL_METADATA_POLICY
from scripts.eval_harness.validator import validate_shape

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / ".agents/reviewer"
GENERIC_RULES = (
    b"# Security\n## SEC-1: Concrete security consequences\n"
    b"Report newly introduced, actionable security consequences.\n"
    b"# Performance\n## PERF-1: Sustained resource growth\n"
    b"Report sustained resource growth with a concrete failure consequence.\n"
)
AST_POLICY = "code-context-only/drop-commit_sha/v1"
RECEIPT_FIELDS = (
    "call_kind",
    "state",
    "actual_input_tokens",
    "actual_output_tokens",
    "reserved_input_tokens",
    "reserved_output_tokens",
)
APPROVED_CORPUS = "b7c2ad5a5ff606d4985af74191143fbf106e5aeabcd9b96f209397b1075f5396"
APPROVED_SOURCES = "c840440689e9ace778bafcf0d5faa02bc49247be78d16a506512f507330a294b"
APPROVED_CASES = 24
MAX_CAMPAIGN_USD = 3


def write_journal(path: Path, body: dict[str, Any], *, create: bool = False) -> None:
    flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if create else os.O_TRUNC)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise LlmConfigurationError(
            "Campaign journal exists; live rerun is refused"
        ) from None
    with os.fdopen(descriptor, "w") as output:
        json.dump(body, output, indent=2)
        output.flush()
        os.fsync(output.fileno())


def live_runtime_values(config: LlmConfig) -> dict[str, Any]:
    runtime = config.runtime
    values: dict[str, Any] = {
        "schema_version": 1,
        "profiles": {
            ref: {
                "provider": profile.provider,
                "base_url": profile.base_url,
                "allowed_models": list(profile.allowed_models),
                "credential_env": runtime.credential_env[ref],
            }
            for ref, profile in runtime.profiles.items()
        },
        "admission": runtime.admission.model_dump(mode="json"),
    }
    if digest(values) != digest(runtime_values()):
        raise LlmConfigurationError(
            "Live worker manifest must pin the approved v4 runtime "
            "without changing limits, routing or verifier"
        )
    return values


def semantic_context(body: dict[str, Any]) -> str:
    """Exclude only declared wrapper revision identity from code/AST comparison."""

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in value.items()
                if key != "commit_sha"
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return digest(
        normalize({key: body[key] for key in ("files", "related_symbols", "coverage")})
    )


def git_object(kind: str, raw: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(raw)}\0".encode() + raw).hexdigest()


def commit(files: dict[str, bytes], parent: str | None = None) -> str:
    """Real Git object identities for explicitly synthetic immutable wrappers."""

    def tree(entries: dict[str, bytes]) -> str:
        objects: dict[str, bytes] = {}
        directories: dict[str, dict[str, bytes]] = {}
        for path, raw in entries.items():
            name, slash, rest = path.partition("/")
            if slash:
                directories.setdefault(name, {})[rest] = raw
            else:
                objects[name] = (
                    b"100644 "
                    + name.encode()
                    + b"\0"
                    + bytes.fromhex(git_object("blob", raw))
                )
        for name, nested in directories.items():
            objects[name + "/"] = (
                b"40000 " + name.encode() + b"\0" + bytes.fromhex(tree(nested))
            )
        return git_object("tree", b"".join(objects[name] for name in sorted(objects)))

    body = f"tree {tree(files)}\n"
    if parent:
        body += f"parent {parent}\n"
    body += (
        "author QA fixture <qa@example.invalid> 0 +0000\n"
        "committer QA fixture <qa@example.invalid> 0 +0000\n\n"
        "Synthetic public-source wrapper.\n"
    )
    return git_object("commit", body.encode())


def load_sources(directory: Path, dataset: Path) -> tuple[dict[str, Any], str]:
    manifest = json.loads(
        dataset_file(directory, directory / "manifest.json").read_text()
    )
    if manifest["format"] != "qa27-worker-sources/v1" or manifest[
        "original_corpus_sha256"
    ] != dataset_digest(dataset):
        raise ValueError("Source bundle does not attest this corpus")
    for path, expected in manifest["files"].items():
        raw = dataset_file(directory, directory / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Source bundle digest mismatch")
    return manifest, digest(manifest)


def runtime_values(reflections: int = 0) -> dict[str, Any]:
    values: dict[str, Any] = json.loads(
        (ROOT / "deploy/analysis-runtime.staging.json").read_text()
    )
    values["admission"].update(
        prompt_version="v4",
        prompt_digest=PromptBuilder.load(PROMPTS, "v4").prompt_digest,
    )
    values["admission"]["policy"].update(
        schema_version=2,
        reflection={
            "max_reflections_per_run": reflections,
            "max_reflections_per_chunk": 1,
            "failure_mode": "fail_open",
            "verifier_version": "v1",
            "verifier_digest": ReflectionBundle.load(
                PROMPTS / "reflection", "v1"
            ).digest,
        },
    )
    return values


@dataclass
class CaseTransport:
    """Only synthetic GitHub reads and scripted provider HTTP; unknown I/O fails."""

    fixture: Fixture
    sources: dict[str, Any]
    source_root: Path
    external_id: str
    factory: async_sessionmaker[AsyncSession]
    rules: bytes | None = None
    owner: str = "qa27"
    repository_name: str | None = None
    number: int = 1
    calls: list[dict[str, Any]] = field(default_factory=list)
    reads: list[dict[str, str]] = field(default_factory=list)
    base_files: dict[str, bytes] = field(init=False)
    head_files: dict[str, bytes] = field(init=False)
    base: str = field(init=False)
    head: str = field(init=False)
    transport_diff: str = field(init=False)

    def __post_init__(self) -> None:
        self.base_files, self.head_files = {}, {}
        for side, files in (("old", self.base_files), ("new", self.head_files)):
            source = self.sources[side]
            if source is not None:
                raw = dataset_file(
                    self.source_root, self.source_root / source["stored_path"]
                ).read_bytes()
                if git_object("blob", raw) != source["git_blob_sha1"]:
                    raise ValueError("Source blob mismatch")
                files[source["path"]] = raw
        if self.rules is not None:
            self.base_files[".review/rules.md"] = self.rules
            self.head_files[".review/rules.md"] = self.rules
        self.base = commit(self.base_files)
        self.head = commit(self.head_files, self.base)
        self.transport_diff = self.fixture.diff_text
        if "index 0000000..1111111" in self.transport_diff:
            self.transport_diff = self.transport_diff.replace(
                "index 0000000..1111111",
                "index " + "0" * 40 + ".." + self.sources["new"]["git_blob_sha1"],
            )

    @property
    def name(self) -> str:
        return (
            self.repository_name
            or "fixture-" + hashlib.sha256(self.fixture.id.encode()).hexdigest()[:12]
        )

    @property
    def repository(self) -> dict[str, Any]:
        return {
            "id": int(self.external_id),
            "name": self.name,
            "owner": {"login": self.owner},
            "permissions": {"pull": True, "admin": True},
        }

    @property
    def pull(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "state": "open",
            "title": "Benchmark case "
            + hashlib.sha256(self.fixture.id.encode()).hexdigest(),
            "body": "",
            "head": {
                "sha": self.head,
                "ref": "fixture",
                "repo": {"id": int(self.external_id)},
            },
            "base": {
                "sha": self.base,
                "ref": "main",
                "repo": {"id": int(self.external_id)},
            },
        }

    async def respond(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 -- fixed fixture HTTP routes
        if (
            request.url.host == "api.eurouter.ai"
            and request.method == "POST"
            and request.url.path == "/api/v1/chat/completions"
        ):
            body = json.loads(request.content)
            async with self.factory() as session:
                attempts = list(
                    await session.scalars(
                        select(models.ReviewLlmAttempt).where(
                            models.ReviewLlmAttempt.state == "RESERVED"
                        )
                    )
                )
                if not attempts:
                    raise AssertionError("Provider dispatch without committed debit")
            self.calls.append(body)
            reflection = (
                "verdicts"
                in body["response_format"]["json_schema"]["schema"]["properties"]
            )
            answer = (
                {
                    "verdicts": [
                        {"index": index, "verdict": "confirmed", "reason": "verified"}
                        for index in range(
                            len(self.fixture.golden_response["findings"])
                        )
                    ]
                }
                if reflection
                else self.fixture.golden_response
            )
            return httpx.Response(
                200,
                json={
                    "model": "mistral-small-4",
                    "choices": [{"message": {"content": json.dumps(answer)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40},
                },
            )
        if request.url.host != "api.github.com" or request.method != "GET":
            raise AssertionError("Worker benchmark attempted unauthorized transport")
        path = request.url.path
        self.reads.append({"path": path, "ref": request.url.params.get("ref", "")})
        if path in {
            f"/repositories/{self.external_id}",
            f"/repos/{self.owner}/{self.name}",
        }:
            return httpx.Response(200, json=self.repository)
        if path == f"/repos/{self.owner}/{self.name}/pulls/{self.number}":
            if request.headers.get("accept") == "application/vnd.github.diff":
                return httpx.Response(200, text=self.transport_diff)
            return httpx.Response(200, json=self.pull)
        if path == f"/repos/{self.owner}/{self.name}/compare/{self.base}...{self.head}":
            if request.headers.get("accept") == "application/vnd.github.diff":
                return httpx.Response(200, text=self.transport_diff)
            return httpx.Response(200, json={"merge_base_commit": {"sha": self.base}})
        if path in {
            f"/repos/{self.owner}/{self.name}/commits/{self.base}",
            f"/repos/{self.owner}/{self.name}/commits/{self.head}",
        }:
            return httpx.Response(200, json={"sha": path.rsplit("/", 1)[1]})
        prefix = f"/repos/{self.owner}/{self.name}/contents/"
        if path.startswith(prefix):
            revision = request.url.params.get("ref")
            if revision not in {self.base, self.head}:
                raise AssertionError("Unpinned source request")
            files = self.base_files if revision == self.base else self.head_files
            name = path.removeprefix(prefix)
            raw = files.get(name)
            if raw is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "size": len(raw),
                    "sha": git_object("blob", raw),
                    "content": base64.b64encode(raw).decode(),
                },
            )
        raise AssertionError(f"Unscripted public fixture path: {path}")


async def process_review(
    factory: async_sessionmaker[AsyncSession],
    queue: RedisQueue,
    runtime: TrustedRuntimeConfiguration,
    transport: httpx.AsyncBaseTransport,
    *,
    provider_http: httpx.AsyncClient | None = None,
) -> bool:
    """Reuse the prepared production worker; no alternate processing lifecycle."""
    await PostgresOutboxDispatcher(factory, queue).dispatch_batch()
    async with httpx.AsyncClient(transport=transport) as http:
        worker = await prepare_postgres_analysis_worker(
            factory,
            queue,
            runtime=runtime,
            github=GithubClient(http),
            http=provider_http or http,
            prompt_root=PROMPTS,
            schemas_dir=ROOT / "schemas",
            owner="qa27",
            environ=None
            if provider_http
            else {"LLM__EUROROUTER__API_KEYS": "synthetic-only"},
        )
        acknowledged = await worker.run_once()
        if not acknowledged and provider_http is None:
            raise AssertionError(
                "Scripted worker did not acknowledge its durable decision"
            )
        return acknowledged


async def cleanup_campaign(
    factory: async_sessionmaker[AsyncSession],
    repositories: list[UUID],
    actors: list[UUID],
) -> None:
    """Remove only this driver's UUID graph; reports keep serialized evidence."""
    if not repositories:
        return
    async with factory.begin() as session:
        reviews = select(models.ReviewJob.id).where(
            models.ReviewJob.repository_id.in_(repositories)
        )
        chunks = select(models.ChunkResult.id).where(
            models.ChunkResult.review_job_id.in_(reviews)
        )
        await session.execute(
            delete(models.Finding).where(models.Finding.chunk_result_id.in_(chunks))
        )
        for table in (
            models.ReviewLlmAttempt,
            models.ReviewEvent,
            models.IdempotencyRecord,
            models.OutboxEvent,
            models.Publication,
            models.ChunkResult,
            models.ContextPayload,
            models.TaskLease,
        ):
            await session.execute(delete(table).where(table.review_job_id.in_(reviews)))
        for repository_table in (
            models.ReviewJob,
            models.QuotaUsage,
            models.ChangeRequest,
            models.RepositoryAccess,
        ):
            await session.execute(
                delete(repository_table).where(
                    repository_table.repository_id.in_(repositories)
                )
            )
        await session.execute(
            update(models.Repository)
            .where(models.Repository.id.in_(repositories))
            .values(current_settings_id=None)
        )
        await session.execute(
            delete(models.RepositorySettings).where(
                models.RepositorySettings.repository_id.in_(repositories)
            )
        )
        await session.execute(
            delete(models.Repository).where(models.Repository.id.in_(repositories))
        )
        await session.execute(
            delete(models.GithubIdentity).where(
                models.GithubIdentity.user_id.in_(actors)
            )
        )
        await session.execute(delete(models.User).where(models.User.id.in_(actors)))


async def run_worker(  # noqa: PLR0912, PLR0913, PLR0915 -- explicit modes/resources for one bounded campaign
    fixtures: list[Fixture],
    dataset: Path,
    source_root: Path,
    *,
    passes: tuple[str, ...] = ("baseline",),
    live_config: LlmConfig | None = None,
    journal_path: Path | None = None,
    provider_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Controlled scripted passes; original runtime budgets and corpus stay fixed."""
    manifest, bundle_digest = load_sources(source_root, dataset)
    db_url = os.environ.get("DMC268_TEST_DATABASE_URL")
    redis_url = os.environ.get("DMC268_TEST_REDIS_URL")
    if not db_url or not redis_url:
        raise ValueError(
            "Worker scope requires disposable PostgreSQL and Redis test URLs"
        )
    common_values = (
        live_runtime_values(live_config) if live_config else runtime_values()
    )
    journal: dict[str, Any] = {}
    if live_config:
        if (
            journal_path is None
            or len(fixtures) != APPROVED_CASES
            or passes != ("baseline", "rules", "reflection")
            or dataset_digest(dataset) != APPROVED_CORPUS
            or hashlib.sha256((source_root / "manifest.json").read_bytes()).hexdigest()
            != APPROVED_SOURCES
        ):
            raise LlmConfigurationError(
                "Live worker requires the approved complete public corpus "
                "and attested sources"
            )
        limits = live_config.runtime.admission.policy.limits
        worst = live_config.cost(
            limits.run_max_input_tokens * len(fixtures),
            limits.run_max_output_tokens * len(fixtures),
        )
        if (
            live_config.cap > 1
            or worst * len(passes) > MAX_CAMPAIGN_USD
            or min(live_config.input_price, live_config.output_price) <= 0
        ):
            raise LlmConfigurationError(
                "Live worker approval ceilings require positive known prices, "
                "at most $1/pass and $3/campaign"
            )
        journal = {
            "state": "running",
            "runtime_manifest_sha256": live_config.runtime.identity,
            "dataset_sha256": APPROVED_CORPUS,
            "source_bundle_sha256": bundle_digest,
            "reserved_upper_bound_usd": str(worst * len(passes)),
            "price_ceilings": {
                "input": str(live_config.input_price),
                "output": str(live_config.output_price),
            },
            "passes": {
                name: {"reserved_upper_bound_usd": str(worst), "completed_cases": 0}
                for name in passes
            },
            "jobs": [],
            "repositories": [],
            "actors": [],
        }
        write_journal(journal_path, journal, create=True)
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repositories: list[UUID] = []
    actors: list[UUID] = []
    scores, rows = [], []
    current_scenario: CaseTransport | None = None

    async def observe_provider(request: httpx.Request) -> None:
        if (
            str(request.url) != "https://api.eurouter.ai/api/v1/chat/completions"
            or request.method != "POST"
            or current_scenario is None
        ):
            raise LlmConfigurationError("Unapproved live provider destination")
        current_scenario.calls.append(json.loads(request.content))

    provider_http = (
        httpx.AsyncClient(
            transport=provider_transport,
            follow_redirects=False,
            event_hooks={"request": [observe_provider]},
        )
        if live_config
        else None
    )
    complete = False
    try:
        async with Redis.from_url(redis_url, decode_responses=True) as redis:
            prefix = "dmc268:qa27:" + uuid4().hex
            queue = RedisQueue(redis, stream_prefix=prefix)
            try:
                for fixture in fixtures:
                    actor, repository_id, settings_id = uuid4(), uuid4(), uuid4()
                    repositories.append(repository_id)
                    actors.append(actor)
                    if live_config:
                        journal["repositories"].append(str(repository_id))
                        journal["actors"].append(str(actor))
                    external_id = str(uuid4().int % (10**15) + 1)
                    key = Fernet.generate_key()
                    settings = Settings(  # type: ignore[call-arg]  # BaseSettings runtime option
                        _env_file=None,
                        github=Github(token=SecretStr("synthetic-bot")),
                        auth=Auth(credential_key=SecretStr(key.decode())),
                    )
                    name = (
                        "fixture-"
                        + hashlib.sha256(fixture.id.encode()).hexdigest()[:12]
                    )
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
                                repository_id=repository_id, user_id=actor, role="admin"
                            )
                        )
                        session.add(
                            models.RepositorySettings(
                                id=settings_id,
                                repository_id=repository_id,
                                rules={"instructions": []},
                                ignores={"globs": []},
                                rules_digest=compute_rules_digest({"instructions": []}),
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
                        repository = await session.get(models.Repository, repository_id)
                        assert repository is not None
                        repository.current_settings_id = settings_id
                    comparison_ast = None
                    for pass_name in passes:
                        scenario = CaseTransport(
                            fixture,
                            manifest["fixtures"][fixture.id],
                            source_root,
                            external_id,
                            factory,
                            rules=None if pass_name == "baseline" else GENERIC_RULES,
                        )
                        current_scenario = scenario
                        values = json.loads(json.dumps(common_values))
                        values["admission"]["policy"]["reflection"][
                            "max_reflections_per_run"
                        ] = int(pass_name == "reflection")
                        with TemporaryDirectory() as temporary:
                            runtime_path = Path(temporary) / "runtime.json"
                            runtime_path.write_text(json.dumps(values))
                            runtime = load_runtime_configuration(runtime_path)
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
                                if live_config:
                                    journal["jobs"].append(
                                        {
                                            "pass": pass_name,
                                            "fixture_id": fixture.id,
                                            "review_id": str(review_id),
                                        }
                                    )
                                    assert journal_path is not None
                                    write_journal(journal_path, journal)
                                await process_review(
                                    factory,
                                    queue,
                                    runtime,
                                    httpx.MockTransport(scenario.respond),
                                    provider_http=provider_http,
                                )
                        async with factory() as session:
                            chunk = (
                                await session.scalars(
                                    select(models.ChunkResult).where(
                                        models.ChunkResult.review_job_id == review_id
                                    )
                                )
                            ).one_or_none()
                            findings = (
                                list(
                                    await session.scalars(
                                        select(models.Finding).where(
                                            models.Finding.chunk_result_id == chunk.id
                                        )
                                    )
                                )
                                if chunk is not None
                                else []
                            )
                            context = (
                                await session.scalars(
                                    select(models.ContextPayload).where(
                                        models.ContextPayload.review_job_id == review_id
                                    )
                                )
                            ).one()
                            job = await session.get(models.ReviewJob, review_id)
                            assert context is not None and job is not None
                            effective = await session.get(
                                models.RepositorySettings, job.repository_settings_id
                            )
                            assert (
                                effective is not None
                                and effective.rules_source is not None
                            )
                            attempts = list(
                                await session.scalars(
                                    select(models.ReviewLlmAttempt)
                                    .where(
                                        models.ReviewLlmAttempt.review_job_id
                                        == review_id
                                    )
                                    .order_by(models.ReviewLlmAttempt.reserved_at)
                                )
                            )
                            if live_config and any(
                                attempt.actual_input_tokens is None
                                or attempt.actual_output_tokens is None
                                for attempt in attempts
                            ):
                                journal["uncertain_usage_receipts"] = [
                                    {
                                        name: getattr(attempt, name)
                                        for name in RECEIPT_FIELDS
                                    }
                                    for attempt in attempts
                                ]
                                raise LlmConfigurationError(
                                    "Live worker usage is uncertain; durable receipts "
                                    "retained and campaign replay refused"
                                )
                            canonical_schema = json.loads(
                                (ROOT / "schemas/review-result/v2.json").read_text()
                            )
                            canonical = (
                                {
                                    "schema_version": chunk.schema_version,
                                    "summary": chunk.summary,
                                    "limitations": chunk.limitations,
                                    "usage": chunk.usage,
                                    "latency_ms": chunk.latency_ms,
                                    "findings": [
                                        {
                                            name: getattr(finding, name)
                                            for name in canonical_schema["$defs"][
                                                "finding"
                                            ]["properties"]
                                        }
                                        for finding in findings
                                    ],
                                }
                                if chunk is not None
                                else None
                            )
                            canonical_valid = (
                                canonical is not None
                                and not validate_shape(canonical, canonical_schema)
                            )
                            projected = (
                                {
                                    name: canonical[name]
                                    for name in ("summary", "findings", "limitations")
                                }
                                if canonical_valid and canonical is not None
                                else None
                            )
                            score = evaluate_fixture(
                                fixture,
                                projected,
                                json.loads(
                                    PromptBuilder.load(PROMPTS, "v4").output_schema_json
                                ),
                            )
                            scores.append(score)
                            files = context.payload_body["files"]
                            ast_digest = semantic_context(context.payload_body)
                            if (
                                comparison_ast is not None
                                and comparison_ast != ast_digest
                            ):
                                raise ValueError(
                                    "Controlled comparison changed code/AST semantics"
                                )
                            comparison_ast = ast_digest
                            messages = json.dumps(
                                [call["messages"] for call in scenario.calls],
                                ensure_ascii=False,
                            )
                            if (
                                fixture.id in messages
                                or fixture.title in messages
                                or (
                                    fixture.description
                                    and fixture.description in messages
                                )
                            ):
                                raise ValueError(
                                    "Ground-truth metadata leaked into model input"
                                )
                            rows.append(
                                {
                                    **score.to_dict(),
                                    "pass": pass_name,
                                    "canonical_result_valid": canonical_valid,
                                    "canonical_result": canonical,
                                    "worker_status": job.status,
                                    "requests": len(scenario.calls),
                                    "ast": {
                                        "source_window_count": sum(
                                            len(file["source_windows"])
                                            for file in files
                                        ),
                                        "whole_file_count": sum(
                                            file["whole_file"] is not None
                                            for file in files
                                        ),
                                        "semantic_sha256": ast_digest,
                                        "normalization_policy": AST_POLICY,
                                    },
                                    "review_id": str(review_id),
                                    "source": {
                                        "base": scenario.base,
                                        "head": scenario.head,
                                        "transport_diff_sha256": hashlib.sha256(
                                            scenario.transport_diff.encode()
                                        ).hexdigest(),
                                    },
                                    "rules_source_state": effective.rules_source[
                                        "file"
                                    ]["state"],
                                    "rules_digest": effective.rules_digest,
                                    "rules_source": effective.rules_source,
                                    "frozen_policy": job.model_settings,
                                    "model_messages_sha256": hashlib.sha256(
                                        messages.encode()
                                    ).hexdigest(),
                                    "usage_receipts": [
                                        {
                                            name: getattr(attempt, name)
                                            for name in RECEIPT_FIELDS
                                        }
                                        for attempt in attempts
                                    ],
                                }
                            )
                            if live_config:
                                journal["passes"][pass_name]["completed_cases"] += 1
            finally:
                await redis.delete(prefix + ":analyze", prefix + ":publish")
        complete = True
    finally:
        try:
            if live_config:
                journal["state"] = "complete" if complete else "uncertain"
                assert journal_path is not None
                write_journal(journal_path, journal)
            else:
                await cleanup_campaign(factory, repositories, actors)
        finally:
            if provider_http:
                await provider_http.aclose()
            await engine.dispose()
    report = build_report(scores)
    report["fixtures"] = rows
    report["passes"] = {
        name: build_report(
            [
                score
                for score, row in zip(scores, rows, strict=True)
                if row["pass"] == name
            ]
        )
        for name in passes
    }
    report["controlled_comparison"] = {
        "valid": True,
        "passes": list(passes),
        "varied_fields": [
            "root base rules and wrapper commit identity",
            "Reflection run allowance 0/1",
        ],
        "code_and_ast_fixed": True,
    }
    report["provenance"] = {
        "scope": "worker-integration",
        "mode": "live" if live_config else "deterministic",
        "quality_measurement": bool(
            live_config
            and provider_transport is None
            and any(row["requests"] for row in rows)
        ),
        "dataset_sha256": dataset_digest(dataset),
        "source_bundle_sha256": bundle_digest,
        "model_metadata_policy": MODEL_METADATA_POLICY,
        "prompt_version": "v4",
        "prompt_digest": common_values["admission"]["prompt_digest"],
        "verifier_digest": common_values["admission"]["policy"]["reflection"][
            "verifier_digest"
        ],
        "runtime_selection_sha256": digest(common_values),
        "model": common_values["admission"]["primary"]["model_ref"],
        "cost_basis": "scripted HTTP receipts, not model quality or invoice",
    }
    report["provenance"].update(
        matching_policy=MATCHING_POLICY,
        provider_schema_sha256=digest(
            json.loads(PromptBuilder.load(PROMPTS, "v4").output_schema_json)
        ),
        canonical_schema_sha256=digest(
            json.loads((ROOT / "schemas/review-result/v2.json").read_text())
        ),
        lockfile_sha256=hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        selected_fixture_ids=[fixture.id for fixture in fixtures],
        passes=list(passes),
    )
    if live_config:
        report["provenance"].update(
            runtime_manifest_sha256=live_config.runtime.identity,
            provider_transport="injected-test"
            if provider_transport is not None
            else "real-provider",
            cost_basis=(
                "price ceilings times charged actual tokens or unknown reservations; "
                "estimate, not invoice"
            ),
            max_cost_usd_per_pass=str(live_config.cap),
            reserved_upper_bound_usd=journal["reserved_upper_bound_usd"],
        )
    report["provenance"]["config_sha256"] = digest(report["provenance"])
    receipts = [receipt for row in rows for receipt in row["usage_receipts"]]
    report["usage"] = {
        "requests": sum(row["requests"] for row in rows),
        "cost_usd": "0",
        "unknown_attempts": sum(
            receipt["actual_input_tokens"] is None
            or receipt["actual_output_tokens"] is None
            for receipt in receipts
        ),
        **{
            f"known_{side}_tokens": sum(
                receipt[f"actual_{side}_tokens"] or 0 for receipt in receipts
            )
            for side in ("input", "output")
        },
        **{
            f"charged_{side}_tokens": sum(
                receipt[f"actual_{side}_tokens"]
                if receipt[f"actual_{side}_tokens"] is not None
                else receipt[f"reserved_{side}_tokens"]
                for receipt in receipts
            )
            for side in ("input", "output")
        },
    }
    if live_config:
        report["usage"]["cost_usd"] = str(
            live_config.cost(
                report["usage"]["charged_input_tokens"],
                report["usage"]["charged_output_tokens"],
            )
        )
        journal["usage"] = report["usage"]
        assert journal_path is not None
        write_journal(journal_path, journal)
    return report
