from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.application.ports import StartReviewCommand
from app.application.review_start_ports import RepositoryIdentity
from app.application.use_cases.start_review import compute_config_digest
from app.db.models import ReviewJob
from app.domain.frozen_configuration import (
    FrozenConfigurationError,
    ModelRoute,
    ModelSettings,
    admission_policy,
)
from app.domain.settings import (
    compute_repository_settings_digest,
    compute_rules_digest,
)
from app.review.configuration import (
    FrozenAdmissionResolver,
    FrozenConfigurationReader,
    RuntimeProfile,
)
from app.review.prompt import PromptBuilder, ReflectionBundle


async def test_admission_and_reader_enforce_pinned_verifier_before_inference() -> None:
    root = Path(".agents/reviewer")
    repository_id, settings_id, review_id, change_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    settings = SimpleNamespace(
        id=settings_id,
        repository_id=repository_id,
        rules={"instructions": []},
        ignores={"globs": []},
        output_language="ru",
        surrounding_lines=3,
        rules_source=None,
    )
    settings.rules_digest = compute_rules_digest(settings.rules)
    settings_digest = compute_repository_settings_digest(
        id=settings_id,
        repository_id=repository_id,
        rules=settings.rules,
        ignores=settings.ignores,
        output_language="ru",
        surrounding_lines=3,
    )
    verifier_digest = ReflectionBundle.load(root / "reflection", "v1").digest
    reflection = {
        "max_reflections_per_run": 1,
        "max_reflections_per_chunk": 1,
        "failure_mode": "fail_open",
        "verifier_version": "v1",
        "verifier_digest": verifier_digest,
    }
    values = admission_policy(settings_digest).model_dump(mode="json") | {
        "schema_version": 2,
        "reflection": reflection,
    }
    profiles = {
        "eurorouter/review-v1": RuntimeProfile(
            provider="eurorouter",
            base_url="https://fake.invalid/v1",
            api_keys=("synthetic",),
            allowed_models=("test-model",),
        )
    }
    primary = ModelRoute(
        profile_ref="eurorouter/review-v1",
        model_ref="test-model",
        model_digest=None,
    )
    job = SimpleNamespace(
        id=review_id,
        repository_id=repository_id,
        repository_settings_id=settings_id,
        change_request_id=change_id,
        requested_head_sha="a" * 40,
        rules_digest=settings.rules_digest,
        trigger_type="manual",
        initiator_user_id=None,
        model_ref="test-model",
        model_digest=None,
        model_settings=values,
        prompt_version="v4",
        prompt_digest=PromptBuilder.load(root, "v4").prompt_digest,
        frozen_configuration_version=1,
    )

    def rehash() -> None:
        job.config_digest = compute_config_digest(
            StartReviewCommand(
                change_request_id=change_id,
                repository_settings_id=settings_id,
                requested_head_sha=job.requested_head_sha,
                rules_digest=job.rules_digest,
                trigger_type="manual",
                initiator_user_id=None,
                model_ref="test-model",
                model_digest=None,
                model_settings=job.model_settings,
                prompt_version=job.prompt_version,
                prompt_digest=job.prompt_digest,
                trace_id="pure-probe",
            )
        )

    class Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            pass

        async def get(self, model: Any, _identifier: Any) -> Any:
            return job if model is ReviewJob else settings

    class Factory:
        def __call__(self) -> Any:
            return Session()

    factory: Any = Factory()
    repository = RepositoryIdentity(repository_id, "github", "42", "owner", "repo")
    rehash()
    reader = FrozenConfigurationReader(factory, root, profiles)
    valid = await reader.resolve(review_id)
    assert valid.verifier is not None
    assert valid.verifier.digest == verifier_digest
    with pytest.raises(FrozenConfigurationError, match="unsupported_verifier_bundle"):
        replace(valid, verifier=None)

    valid_resolver = FrozenAdmissionResolver(
        factory,
        root,
        profiles,
        primary=primary,
        policy=ModelSettings.from_stored(values),
        prompt_version="v4",
    )
    valid_admission = await valid_resolver.resolve(
        repository=repository,
        settings_id=settings_id,
        rules_digest=settings.rules_digest,
    )
    assert valid_admission.model_settings["reflection"] == reflection

    values["reflection"] = reflection | {"verifier_digest": "0" * 64}
    rehash()
    try:
        await reader.resolve(review_id)
    except FrozenConfigurationError as error:
        assert error.reason == "unsupported_prompt_bundle"
    else:
        raise AssertionError("reader accepted wrong verifier digest")

    resolver = FrozenAdmissionResolver(
        factory,
        root,
        profiles,
        primary=primary,
        policy=ModelSettings.from_stored(values),
        prompt_version="v4",
    )
    try:
        await resolver.resolve(
            repository=repository,
            settings_id=settings_id,
            rules_digest=settings.rules_digest,
        )
    except FrozenConfigurationError as error:
        assert error.reason == "unsupported_prompt_bundle"
    else:
        raise AssertionError(
            "FrozenAdmissionResolver accepted wrong verifier_digest; "
            "expected unsupported_prompt_bundle before admission"
        )
