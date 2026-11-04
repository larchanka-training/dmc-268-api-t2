"""Frozen, durable execution composition. No runtime model/policy defaults."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.analysis_authority import (
    LiveAnalysisAuthority,
    current_analysis_authority,
)
from app.application.ports import LeaseLostError, QueueTaskEnvelope, TaskLeaseSnapshot
from app.db.analysis_authority import lock_analysis
from app.db.llm_accounting import PostgresReviewRunBudget
from app.domain.frozen_configuration import RepositoryInstructions
from app.llm.tokens import ConservativeTokenCounter
from app.review.configuration import (
    ExecutionConfiguration,
    FrozenConfigurationReader,
    RuntimeProfile,
    build_frozen_gateway,
)
from app.review.port_adapter import LlmGatewayAdapter
from app.review.prompt import PromptBuilder
from app.review.service import ReviewService
from app.review.validation import FindingValidator


@dataclass(frozen=True, slots=True)
class FrozenAnalysisExecution:
    configuration: ExecutionConfiguration
    llm: LlmGatewayAdapter


class FrozenAnalysisExecutionFactory:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        prompt_root: Path,
        profiles: Mapping[str, RuntimeProfile],
        http: httpx.AsyncClient,
    ) -> None:
        self._factory = factory
        self._prompts = prompt_root
        self._profiles = MappingProxyType(dict(profiles))
        self._http = http
        self._reader = FrozenConfigurationReader(factory, prompt_root, self._profiles)

    async def create(
        self,
        *,
        task: QueueTaskEnvelope,
        lease: TaskLeaseSnapshot,
        analysis_attempt: int,
        authority: LiveAnalysisAuthority,
    ) -> FrozenAnalysisExecution:
        if current_analysis_authority.get() is not authority or (
            authority.lease.review_job_id,
            authority.lease.task_kind,
            authority.lease.owner,
            authority.lease.fencing_token,
        ) != (
            lease.review_job_id,
            lease.task_kind,
            lease.owner,
            lease.fencing_token,
        ):
            raise LeaseLostError("Execution authority is not current")
        authority.check()
        async with self._factory.begin() as session:
            job, _, _ = await lock_analysis(session, task, lease)
            if (
                job.analysis_attempts != analysis_attempt
                or job.analysis_attempt_fence != lease.fencing_token
            ):
                raise LeaseLostError("Analysis attempt is not current")
            config = await self._reader.resolve(job.id)
        authority.check()
        counter = ConservativeTokenCounter()
        prompts = PromptBuilder.load(
            self._prompts,
            config.prompt_version,
            expected_digest=config.prompt_digest,
            repository_instructions=RepositoryInstructions(
                instructions=config.instructions
            ),
        )
        # Deliberately no budget argument/default: every instance reads this review's
        # authoritative ledger, across workers, contexts, retries and service rebuilds.
        budget = PostgresReviewRunBudget(
            self._factory, task=task, lease=lease, analysis_attempt=analysis_attempt
        )
        service = ReviewService(
            build_frozen_gateway(config, self._profiles, self._http),
            prompts,
            FindingValidator(),
            counter,
            config.service_settings,
            frozen_configuration=config,
        )
        return FrozenAnalysisExecution(config, LlmGatewayAdapter(service, budget))
