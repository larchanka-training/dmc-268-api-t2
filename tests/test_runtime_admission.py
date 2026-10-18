"""Trusted API selection freezes configuration without execution credentials."""

import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness
from test_review_start_api import HEAD, HEADERS, FakeStartGithub, StartScope
from test_runtime_manifest import PROMPTS, manifest_values, write_manifest

from app.application.review_start_ports import RepositoryIdentity
from app.domain.settings import compute_rules_digest
from app.review.configuration import load_runtime_configuration

pytest_plugins = ("test_review_start_api",)


async def test_trusted_eurorouter_admission_requires_no_llm_secrets(
    postgres: PostgresHarness,
    scope: StartScope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("LLM__"):
            monkeypatch.delenv(name)
    runtime = load_runtime_configuration(write_manifest(tmp_path, manifest_values()))
    assert runtime.profiles["eurorouter/review-v1"].api_keys == ()
    resolver = runtime.admission_resolver(async_sessionmaker(postgres.engine), PROMPTS)
    result = await resolver.resolve(
        repository=RepositoryIdentity(
            scope.repository, "github", "123456", "owner", "repository"
        ),
        settings_id=scope.settings,
        rules_digest=compute_rules_digest({"instructions": []}),
    )
    assert result.model_ref == "test-model"
    assert result.prompt_digest == runtime.admission.prompt_digest


async def test_http_uses_trusted_manifest_without_llm_credentials(
    client: httpx.AsyncClient,
    scope: StartScope,
    providers: FakeStartGithub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM__EUROROUTER__API_KEYS", raising=False)
    monkeypatch.setenv(
        "ANALYSIS__CONFIG_PATH", str(write_manifest(tmp_path, manifest_values()))
    )
    response = await client.post(
        scope.url, json={"requested_head_sha": HEAD}, headers=HEADERS
    )
    assert response.status_code == 202  # noqa: PLR2004
