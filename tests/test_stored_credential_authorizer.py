"""Worker-side start authorization with the initiator's stored GitHub credential."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import PostgresHarness

from app.application.github_ports import GithubFailure
from app.application.review_start_ports import RepositoryIdentity
from app.core.config import Settings
from app.core.github_runtime import StoredCredentialAuthorizer
from app.db import models

pytest_plugins = ("test_postgres_integration",)
_KEY = Fernet.generate_key().decode()
REPOSITORY = RepositoryIdentity(uuid.uuid4(), "github", "4242", "team", "api")


@pytest_asyncio.fixture
async def user(postgres: PostgresHarness) -> AsyncIterator[uuid.UUID]:
    user_id = uuid.uuid4()
    async with postgres.engine.begin() as connection:
        await connection.execute(insert(models.User).values(id=user_id))
    try:
        yield user_id
    finally:
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(models.GithubIdentity).where(
                    models.GithubIdentity.user_id == user_id
                )
            )
            await connection.execute(
                delete(models.User).where(models.User.id == user_id)
            )


async def _store_token(postgres: PostgresHarness, user: uuid.UUID, token: str) -> None:
    async with postgres.engine.begin() as connection:
        await connection.execute(
            insert(models.GithubIdentity).values(
                user_id=user,
                external_id=str(user.int),
                login="octocat",
                encrypted_token=Fernet(_KEY).encrypt(token.encode()).decode(),
            )
        )


def _authorizer(
    postgres: PostgresHarness, reply: httpx.Response, seen: list[httpx.Request]
) -> StoredCredentialAuthorizer:
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply

    settings = Settings.model_validate(
        {"auth": {"credential_key": _KEY}, "github": {"token": "bot-token"}}
    )
    return StoredCredentialAuthorizer(
        settings,
        async_sessionmaker(postgres.engine),
        transport=httpx.MockTransport(respond),
    )


@pytest.mark.parametrize(("pull", "allowed"), [(True, True), (False, False)])
async def test_permission_is_read_with_the_users_own_token(
    postgres: PostgresHarness, user: uuid.UUID, pull: bool, allowed: bool
) -> None:
    await _store_token(postgres, user, "user-token")
    seen: list[httpx.Request] = []
    reply = httpx.Response(200, json={"id": 4242, "permissions": {"pull": pull}})
    authorizer = _authorizer(postgres, reply, seen)

    if allowed:
        await authorizer.authorize_start(user_id=user, repository=REPOSITORY)
    else:
        with pytest.raises(GithubFailure) as denied:
            await authorizer.authorize_start(user_id=user, repository=REPOSITORY)
        assert denied.value.reason_code == "VCS_ACCESS_DENIED"
    assert [request.headers["authorization"] for request in seen] == [
        "Bearer user-token"
    ]
    assert str(seen[0].url) == "https://api.github.com/repositories/4242"


async def test_missing_credential_cannot_prove_access(
    postgres: PostgresHarness, user: uuid.UUID
) -> None:
    seen: list[httpx.Request] = []
    authorizer = _authorizer(postgres, httpx.Response(200, json={}), seen)

    with pytest.raises(GithubFailure) as denied:
        await authorizer.authorize_start(user_id=user, repository=REPOSITORY)

    assert (denied.value.reason_code, denied.value.retryable) == (
        "VCS_ACCESS_DENIED",
        False,
    )
    assert seen == []  # never falls back to the bot token


async def test_provider_outage_stays_retryable(
    postgres: PostgresHarness, user: uuid.UUID
) -> None:
    await _store_token(postgres, user, "user-token")
    authorizer = _authorizer(postgres, httpx.Response(503), [])

    with pytest.raises(GithubFailure) as failure:
        await authorizer.authorize_start(user_id=user, repository=REPOSITORY)

    assert failure.value.retryable
