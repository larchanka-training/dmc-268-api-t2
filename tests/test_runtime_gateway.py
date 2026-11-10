"""The direct frozen Gateway builder cannot bypass execution credentials."""

import uuid
from dataclasses import replace

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_analysis_terminal import PROFILES, PROMPTS, freeze
from test_frozen_configuration import runtime_profiles, save_policy
from test_postgres_integration import PostgresHarness

from app.core.circuit_breaker import BreakerRegistry
from app.domain.frozen_configuration import FrozenConfigurationError
from app.review.configuration import FrozenConfigurationReader, build_frozen_gateway

pytest_plugins = ("test_analysis_handler",)


@pytest.mark.parametrize("location", ["primary", "fallback"])
@pytest.mark.parametrize("keys", [(), ("",), ("bad\nkey",), ("valid-test-key", " ")])
async def test_direct_frozen_gateway_refuses_credentials_before_transport(
    postgres: PostgresHarness,
    review_id: uuid.UUID,
    location: str,
    keys: tuple[str, ...],
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    config = await FrozenConfigurationReader(factory, PROMPTS, PROFILES).resolve(
        review_id
    )
    profiles = dict(PROFILES)
    if location == "fallback":
        values = config.policy.model_dump(mode="json")
        values["routing"] = {
            "primary_profile_ref": "ollama/review-v1",
            "fallback": [
                {
                    "profile_ref": "eurorouter/review-v1",
                    "model_ref": "test-model",
                    "model_digest": None,
                }
            ],
        }
        # This inventory's Ollama descriptor is deliberately auth-free.
        profiles.update(runtime_profiles())
        await save_policy(postgres, review_id, model_settings=values)
    profiles["eurorouter/review-v1"] = replace(
        profiles["eurorouter/review-v1"], api_keys=keys
    )
    config = await FrozenConfigurationReader(factory, PROMPTS, profiles).resolve(
        review_id
    )
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("credential failure must precede HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(
            FrozenConfigurationError, match="invalid_execution_credentials"
        ):
            build_frozen_gateway(config, profiles, http)
    assert calls == []


async def test_frozen_gateway_pools_share_process_breakers_by_profile(
    postgres: PostgresHarness, review_id: uuid.UUID
) -> None:
    factory = async_sessionmaker(postgres.engine, expire_on_commit=False)
    await freeze(factory, review_id)
    config = await FrozenConfigurationReader(factory, PROMPTS, PROFILES).resolve(
        review_id
    )
    breakers = BreakerRegistry()

    async with httpx.AsyncClient() as http:
        first = build_frozen_gateway(config, PROFILES, http, breakers=breakers)
        second = build_frozen_gateway(config, PROFILES, http, breakers=breakers)

    assert [pool.breaker for pool in first._pools] == [
        breakers.get(pool.profile_ref or "") for pool in second._pools
    ]
    assert all(pool.breaker is not None for pool in first._pools)
