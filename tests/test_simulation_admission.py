"""Public admission seam against an explicitly disposable Redis server."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest
from redis.asyncio import Redis
from redis.client import NEVER_DECODE

from app.adapters.simulation_admission import AdmissionResult, RedisSimulationAdmission

_REDIS_URL = os.environ.get("DMC268_TEST_REDIS_URL")
_OWNER_TOKEN_HEX_LENGTH = 64
_PHYSICAL_TTL_MS = 60_000
if not _REDIS_URL:
    pytest.skip("requires disposable Redis", allow_module_level=True)


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    assert _REDIS_URL is not None
    async with Redis.from_url(_REDIS_URL, decode_responses=True) as client:
        yield client


@pytest.fixture
async def state_key(redis_client: Redis) -> AsyncIterator[str]:
    key = f"dmc268:test:rule-playground:{uuid.uuid4().hex}"
    await redis_client.set(key, '{"schema_version":1,"lease":null}')
    try:
        yield key
    finally:
        await redis_client.delete(key)


@pytest.fixture
async def admissions(
    state_key: str,
) -> AsyncIterator[tuple[RedisSimulationAdmission, RedisSimulationAdmission]]:
    assert _REDIS_URL is not None
    first = RedisSimulationAdmission(_REDIS_URL, state_key=state_key)
    second = RedisSimulationAdmission(_REDIS_URL, state_key=state_key)
    try:
        yield first, second
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.parametrize("_round", range(20))
async def test_only_one_independent_client_acquires_and_busy_preserves_state(
    _round: int,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    # Each adapter owns its own pool/connection; synchronize callers, not admission.
    start = asyncio.Event()

    async def acquire(client: RedisSimulationAdmission) -> AdmissionResult:
        await start.wait()
        return await client.acquire()

    contenders = [asyncio.create_task(acquire(client)) for client in admissions]
    seconds, micros = await redis_client.time()
    start.set()
    results = await asyncio.gather(*contenders)
    assert sorted(result.status for result in results) == ["ACQUIRED", "BUSY"]
    winner = next(result.lease for result in results if result.status == "ACQUIRED")
    assert winner is not None
    after_seconds, after_micros = await redis_client.time()
    assert (
        seconds * 1000 + micros // 1000 + 90_000
        <= winner.expires_at_ms
        <= after_seconds * 1000 + after_micros // 1000 + 90_000
    )
    assert len(winner.owner_token) == _OWNER_TOKEN_HEX_LENGTH  # 256 random bits.
    stored = await redis_client.get(state_key)
    assert stored is not None
    assert json.loads(stored) == {
        "schema_version": 1,
        "lease": {
            "owner_token": winner.owner_token,
            "expires_at_ms": winner.expires_at_ms,
        },
    }
    assert await redis_client.pttl(state_key) == -1
    assert (await admissions[0].acquire()).status == "BUSY"
    assert (await admissions[1].acquire()).status == "BUSY"
    assert await redis_client.get(state_key) == stored


async def test_redis_error_is_unavailable_never_admission() -> None:
    admission = RedisSimulationAdmission("redis://127.0.0.1:1/0")
    try:
        result = await admission.acquire()
        assert result.status == "UNAVAILABLE"
        assert result.lease is None
    finally:
        await admission.aclose()


async def test_owner_safe_release_lifecycle(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    first, second = admissions
    old = (await first.acquire()).lease
    assert old is not None
    stored = await redis_client.get(state_key)
    assert await second.release("f" * 64) == "NOT_OWNER"
    assert await redis_client.get(state_key) == stored
    assert await first.release(old.owner_token) == "RELEASED"
    released = await redis_client.get(state_key)
    assert released is not None
    assert json.loads(released) == {"schema_version": 1, "lease": None}
    assert await first.release(old.owner_token) == "NOT_OWNER"
    assert await redis_client.get(state_key) == released
    new = (await second.acquire()).lease
    assert new is not None and new.owner_token != old.owner_token
    new_state = await redis_client.get(state_key)
    assert await first.release(old.owner_token) == "NOT_OWNER"
    assert await redis_client.get(state_key) == new_state
    assert await second.release(new.owner_token) == "RELEASED"
    assert await redis_client.pttl(state_key) == -1


async def test_release_redis_error_is_unavailable() -> None:
    admission = RedisSimulationAdmission("redis://127.0.0.1:1/0")
    try:
        assert await admission.release("a" * 64) == "UNAVAILABLE"
    finally:
        await admission.aclose()


async def test_expired_lease_is_replaced_and_old_owner_cannot_release_it(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    await redis_client.set(
        state_key,
        json.dumps(
            {
                "schema_version": 1,
                "lease": {
                    "owner_token": "a" * 64,
                    "expires_at_ms": seconds * 1000 + micros // 1000 - 1,
                },
            }
        ),
    )
    result = await admissions[1].acquire()
    assert result.status == "ACQUIRED"
    assert result.lease is not None and result.lease.owner_token != "a" * 64
    stored = await redis_client.get(state_key)
    assert await admissions[0].release("a" * 64) == "NOT_OWNER"
    assert await redis_client.get(state_key) == stored


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(None, id="missing"),
        pytest.param(["unrelated"], id="wrong-redis-type"),
        pytest.param("{broken", id="invalid-json"),
        pytest.param("null", id="null"),
        pytest.param("[]", id="array"),
        pytest.param("42", id="scalar"),
        pytest.param('{"schema_version":2,"lease":null}', id="version"),
        pytest.param('{"schema_version":"1","lease":null}', id="version-type"),
        pytest.param('{"schema_version":true,"lease":null}', id="boolean-version"),
        pytest.param('{"lease":null}', id="missing-version"),
        pytest.param('{"schema_version":1}', id="missing-lease"),
        pytest.param('{"schema_version":1,"lease":false}', id="lease-type"),
        pytest.param('{"schema_version":1,"lease":{}}', id="lease-fields"),
        pytest.param(
            '{"schema_version":1,"lease":null,"extra":"private"}', id="extra-field"
        ),
        pytest.param(" " * 1024 + '{"schema_version":1,"lease":null}', id="oversize"),
        *[
            pytest.param(json.dumps({"schema_version": 1, "lease": lease}), id=label)
            for label, lease in [
                ("owner-type", {"owner_token": 5, "expires_at_ms": 1}),
                ("owner-format", {"owner_token": "z" * 64, "expires_at_ms": 1}),
                ("owner-length", {"owner_token": "a", "expires_at_ms": 1}),
                ("expiry-type", {"owner_token": "a" * 64, "expires_at_ms": "1"}),
                ("expiry-negative", {"owner_token": "a" * 64, "expires_at_ms": -1}),
                ("expiry-fraction", {"owner_token": "a" * 64, "expires_at_ms": 1.5}),
                (
                    "expiry-unsafe-integer",
                    {"owner_token": "a" * 64, "expires_at_ms": 9007199254740992},
                ),
                (
                    "extra-lease-field",
                    {"owner_token": "a" * 64, "expires_at_ms": 1, "extra": 1},
                ),
            ]
        ],
    ],
)
@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_invalid_state_is_rejected_without_any_write(
    operation: str,
    state: str | list[str] | None,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    await redis_client.delete(state_key)
    if isinstance(state, str):
        await redis_client.set(state_key, state)
    elif isinstance(state, list):
        await redis_client.rpush(state_key, *state)
    # Persisted bytes are part of this seam's explicit no-write contract.
    before = await redis_client.execute_command(  # type: ignore[no-untyped-call]
        "DUMP", state_key, **{NEVER_DECODE: True}
    )
    if operation == "acquire":
        result = await admissions[0].acquire()
        assert result.status == "INVALID_STATE"
        assert result.lease is None
    else:
        assert await admissions[1].release("a" * 64) == "INVALID_STATE"
    after = await redis_client.execute_command(  # type: ignore[no-untyped-call]
        "DUMP", state_key, **{NEVER_DECODE: True}
    )
    assert after == before
    assert await redis_client.pttl(state_key) == (-2 if state is None else -1)


@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_root_with_physical_ttl_is_invalid_and_not_repaired(
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    lease = (await admissions[0].acquire()).lease
    assert lease is not None
    await redis_client.pexpire(state_key, _PHYSICAL_TTL_MS)
    before = await redis_client.get(state_key)
    if operation == "acquire":
        assert (await admissions[1].acquire()).status == "INVALID_STATE"
    else:
        assert await admissions[0].release(lease.owner_token) == "INVALID_STATE"
    assert await redis_client.get(state_key) == before
    assert 0 < await redis_client.pttl(state_key) <= _PHYSICAL_TTL_MS


async def test_unexpired_seeded_lease_remains_busy_without_writes(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    state = json.dumps(
        {
            "schema_version": 1,
            "lease": {
                "owner_token": "b" * 64,
                "expires_at_ms": seconds * 1000 + micros // 1000 + 90_000,
            },
        }
    )
    await redis_client.set(state_key, state)
    results = await asyncio.gather(*(client.acquire() for client in admissions))
    assert [result.status for result in results] == ["BUSY", "BUSY"]
    assert await redis_client.get(state_key) == state


async def test_expired_replacement_and_duplicate_release_are_atomic(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    await redis_client.set(
        state_key,
        json.dumps(
            {
                "schema_version": 1,
                "lease": {
                    "owner_token": "c" * 64,
                    "expires_at_ms": seconds * 1000 + micros // 1000 - 1,
                },
            }
        ),
    )
    results = await asyncio.gather(*(client.acquire() for client in admissions))
    assert sorted(result.status for result in results) == ["ACQUIRED", "BUSY"]
    lease = next(result.lease for result in results if result.lease is not None)
    releases = await asyncio.gather(
        *(client.release(lease.owner_token) for client in admissions)
    )
    assert sorted(releases) == ["NOT_OWNER", "RELEASED"]


@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_denied_final_write_leaves_state_unchanged(
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    owner = "a" * 64
    if operation == "release":
        lease = (await admissions[0].acquire()).lease
        assert lease is not None
        owner = lease.owner_token
    before = await redis_client.get(state_key)
    username, password = uuid.uuid4().hex, uuid.uuid4().hex
    await redis_client.acl_setuser(
        username,
        enabled=True,
        passwords=[f"+{password}"],
        commands=["+@all", "-set"],
        keys=[state_key],
    )
    assert _REDIS_URL is not None
    parts = urlsplit(_REDIS_URL)
    url = urlunsplit(parts._replace(netloc=f"{username}:{password}@{parts.netloc}"))
    denied = RedisSimulationAdmission(url, state_key=state_key)
    try:
        if operation == "acquire":
            result = await denied.acquire()
            assert result.status == "UNAVAILABLE"
            assert result.lease is None
        else:
            assert await denied.release(owner) == "UNAVAILABLE"
        assert await redis_client.get(state_key) == before
        assert await redis_client.pttl(state_key) == -1
    finally:
        await denied.aclose()
        await redis_client.acl_deluser(username)
