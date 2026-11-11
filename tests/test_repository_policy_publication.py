# ruff: noqa: RUF001 -- Russian publication contract text
"""Repository policy is enforced at the persisted publication boundary."""

from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from test_github_publisher_postgres import FakeGithub, setup_publication
from test_publications_postgres import (
    publication_engine as publication_engine,  # noqa: PLC0414 -- pytest fixture export
)

from app.application.github_publication import GithubPublisher
from app.db.models import RepositorySettings, ReviewJob
from app.domain.frozen_configuration import admission_policy
from app.domain.repository_policy import StoredReviewPolicy
from app.domain.settings import compute_repository_settings_digest
from app.vcs.github import GithubClient


async def freeze_threshold(
    engine: AsyncEngine, review_id: UUID, threshold: str
) -> None:
    async with engine.begin() as connection:
        settings = (
            (
                await connection.execute(
                    select(RepositorySettings.__table__)
                    .join(
                        ReviewJob,
                        ReviewJob.repository_settings_id == RepositorySettings.id,
                    )
                    .where(ReviewJob.id == review_id)
                )
            )
            .mappings()
            .one()
        )
        policy = StoredReviewPolicy.model_validate(
            {"minimum_severity": threshold}
        ).model_dump(mode="json")
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == settings["id"])
            .values(review_policy=policy)
        )
        checksum = compute_repository_settings_digest(
            id=settings["id"],
            repository_id=settings["repository_id"],
            rules=settings["rules"],
            ignores=settings["ignores"],
            output_language=settings["output_language"],
            surrounding_lines=settings["surrounding_lines"],
            review_policy=policy,
        )
        await connection.execute(
            update(ReviewJob)
            .where(ReviewJob.id == review_id)
            .values(
                frozen_configuration_version=1,
                model_settings=admission_policy(checksum).model_dump(mode="json"),
            )
        )


async def test_publication_excludes_findings_below_frozen_threshold(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    store, authority = await setup_publication(publication_engine, payload_data)
    await freeze_threshold(publication_engine, authority.review_job_id, "critical")
    provider = FakeGithub()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        result = await GithubPublisher(
            store, GithubClient(http), authority=authority, bot_user_id="9002"
        ).publish(review_job_id=authority.review_job_id)
    assert result["status"] == "PUBLISHED"
    assert provider.posts[0]["comments"] == []
    loaded = await store.load(authority.review_job_id)
    assert loaded is not None and len(loaded.findings) == 1


async def test_overview_does_not_leak_excluded_findings_through_model_summary(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    store, authority = await setup_publication(publication_engine, payload_data)
    await freeze_threshold(publication_engine, authority.review_job_id, "critical")
    provider = FakeGithub()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        await GithubPublisher(
            store, GithubClient(http), authority=authority, bot_user_id="9002"
        ).publish(review_job_id=authority.review_job_id)
    body = provider.posts[0]["body"]
    assert "One security risk" not in body
    assert "Validate input" not in body
    assert "critical" in body
    assert "На выбранном уровне серьёзности замечаний нет." in body


async def test_publication_refuses_changed_frozen_policy_before_any_remote_request(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    store, authority = await setup_publication(publication_engine, payload_data)
    await freeze_threshold(publication_engine, authority.review_job_id, "critical")
    async with publication_engine.begin() as connection:
        settings_id = await connection.scalar(
            select(ReviewJob.repository_settings_id).where(
                ReviewJob.id == authority.review_job_id
            )
        )
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == settings_id)
            .values(
                review_policy=StoredReviewPolicy(minimum_severity="low").model_dump(
                    mode="json"
                )
            )
        )
    provider = FakeGithub()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        with pytest.raises(
            LookupError, match="Frozen publication input is unavailable"
        ):
            await GithubPublisher(
                store, GithubClient(http), authority=authority, bot_user_id="9002"
            ).publish(review_job_id=authority.review_job_id)
    assert provider.requests == []


async def test_publication_refuses_erased_frozen_policy_before_remote_reads(
    publication_engine: AsyncEngine,
    payload_data: dict[str, Any],
) -> None:
    store, authority = await setup_publication(publication_engine, payload_data)
    await freeze_threshold(publication_engine, authority.review_job_id, "critical")
    async with publication_engine.begin() as connection:
        settings_id = await connection.scalar(
            select(ReviewJob.repository_settings_id).where(
                ReviewJob.id == authority.review_job_id
            )
        )
        await connection.execute(
            update(RepositorySettings)
            .where(RepositorySettings.id == settings_id)
            .values(review_policy=None)
        )
    provider = FakeGithub()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.respond)
    ) as http:
        with pytest.raises(
            LookupError, match="Frozen publication input is unavailable"
        ):
            await GithubPublisher(
                store, GithubClient(http), authority=authority, bot_user_id="9002"
            ).publish(review_job_id=authority.review_job_id)
    assert provider.requests == []
