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
_USER = uuid.uuid4()
_REPOSITORY = uuid.uuid4()
_WINDOW_MS = 3_600_000
_BOUNDARY_SLACK_MS = 30_000
_MAX_STATE_BYTES = 4096
_USER_LIMIT = 3
_REPOSITORY_LIMIT = 10
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
    await redis_client.set(key, '{"schema_version":2,"lease":null,"admissions":[]}')
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
        return await client.acquire(_USER, _REPOSITORY)

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
        "schema_version": 2,
        "admissions": [
            {
                "at_ms": winner.expires_at_ms - 90_000,
                "user_id": _USER.hex,
                "repository_id": _REPOSITORY.hex,
            }
        ],
        "lease": {
            "owner_token": winner.owner_token,
            "expires_at_ms": winner.expires_at_ms,
        },
    }
    assert await redis_client.pttl(state_key) == -1
    assert (await admissions[0].acquire(_USER, _REPOSITORY)).status == "BUSY"
    assert (await admissions[1].acquire(_USER, _REPOSITORY)).status == "BUSY"
    assert await redis_client.get(state_key) == stored


async def test_redis_error_is_unavailable_never_admission() -> None:
    admission = RedisSimulationAdmission("redis://127.0.0.1:1/0")
    try:
        result = await admission.acquire(_USER, _REPOSITORY)
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
    old = (await first.acquire(_USER, _REPOSITORY)).lease
    assert old is not None
    stored = await redis_client.get(state_key)
    assert await second.release("f" * 64) == "NOT_OWNER"
    assert await redis_client.get(state_key) == stored
    assert await first.release(old.owner_token) == "RELEASED"
    released = await redis_client.get(state_key)
    assert released is not None
    assert stored is not None
    assert json.loads(released) == {**json.loads(stored), "lease": None}
    assert await first.release(old.owner_token) == "NOT_OWNER"
    assert await redis_client.get(state_key) == released
    new = (await second.acquire(_USER, _REPOSITORY)).lease
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
                "schema_version": 2,
                "admissions": [],
                "lease": {
                    "owner_token": "a" * 64,
                    "expires_at_ms": seconds * 1000 + micros // 1000 - 1,
                },
            }
        ),
    )
    result = await admissions[1].acquire(_USER, _REPOSITORY)
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
        pytest.param('{"schema_version":1,"lease":null}', id="version"),
        pytest.param(
            '{"schema_version":"2","lease":null,"admissions":[]}', id="version-type"
        ),
        pytest.param(
            '{"schema_version":true,"lease":null,"admissions":[]}', id="boolean-version"
        ),
        pytest.param('{"lease":null,"admissions":[]}', id="missing-version"),
        pytest.param('{"schema_version":2,"admissions":[]}', id="missing-lease"),
        pytest.param(
            '{"schema_version":2,"lease":false,"admissions":[]}', id="lease-type"
        ),
        pytest.param(
            '{"schema_version":2,"lease":{},"admissions":[]}', id="lease-fields"
        ),
        pytest.param(
            '{"schema_version":2,"lease":null,"admissions":[],"extra":"private"}',
            id="extra-field",
        ),
        pytest.param(
            " " * 4096 + '{"schema_version":2,"lease":null,"admissions":[]}',
            id="oversize",
        ),
        *[
            pytest.param(
                json.dumps({"schema_version": 2, "admissions": [], "lease": lease}),
                id=label,
            )
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
        result = await admissions[0].acquire(_USER, _REPOSITORY)
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
    lease = (await admissions[0].acquire(_USER, _REPOSITORY)).lease
    assert lease is not None
    await redis_client.pexpire(state_key, _PHYSICAL_TTL_MS)
    before = await redis_client.get(state_key)
    if operation == "acquire":
        assert (
            await admissions[1].acquire(_USER, _REPOSITORY)
        ).status == "INVALID_STATE"
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
            "schema_version": 2,
            "admissions": [],
            "lease": {
                "owner_token": "b" * 64,
                "expires_at_ms": seconds * 1000 + micros // 1000 + 90_000,
            },
        }
    )
    await redis_client.set(state_key, state)
    results = await asyncio.gather(
        *(client.acquire(_USER, _REPOSITORY) for client in admissions)
    )
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
                "schema_version": 2,
                "admissions": [],
                "lease": {
                    "owner_token": "c" * 64,
                    "expires_at_ms": seconds * 1000 + micros // 1000 - 1,
                },
            }
        ),
    )
    results = await asyncio.gather(
        *(client.acquire(_USER, _REPOSITORY) for client in admissions)
    )
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
        lease = (await admissions[0].acquire(_USER, _REPOSITORY)).lease
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
            result = await denied.acquire(_USER, _REPOSITORY)
            assert result.status == "UNAVAILABLE"
            assert result.lease is None
        else:
            assert await denied.release(owner) == "UNAVAILABLE"
        assert await redis_client.get(state_key) == before
        assert await redis_client.pttl(state_key) == -1
    finally:
        await denied.aclose()
        await redis_client.acl_deluser(username)


async def test_user_fourth_admission_is_denied_after_release_without_writes(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    for index in range(3):
        client = admissions[index % 2]
        result = await client.acquire(_USER, _REPOSITORY)
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await client.release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    denied = await admissions[1].acquire(_USER, uuid.uuid4())
    assert denied.status == "RATE_USER"
    assert denied.lease is None
    assert await redis_client.get(state_key) == before
    # Denial did not create a lease or consume another user's allowance.
    other = await admissions[0].acquire(uuid.uuid4(), _REPOSITORY)
    assert other.status == "ACQUIRED"
    assert other.lease is not None
    assert await admissions[0].release(other.lease.owner_token) == "RELEASED"


async def test_repository_eleventh_admission_is_denied_across_users_and_pools(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    for index in range(10):
        client = admissions[index % 2]
        result = await client.acquire(uuid.uuid4(), _REPOSITORY)
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await client.release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    denied_user = uuid.uuid4()
    denied = await admissions[1].acquire(denied_user, _REPOSITORY)
    assert denied.status == "RATE_REPOSITORY"
    assert denied.lease is None
    assert await redis_client.get(state_key) == before
    # Same user can still spend all three admissions on another repository.
    for client in (admissions[0], admissions[1], admissions[0]):
        result = await client.acquire(denied_user, uuid.uuid4())
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await client.release(result.lease.owner_token) == "RELEASED"


async def test_global_twenty_first_admission_is_denied_across_pools(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    for index in range(20):
        client = admissions[index % 2]
        result = await client.acquire(uuid.uuid4(), uuid.uuid4())
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await client.release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    for client in reversed(admissions):
        denied = await client.acquire(uuid.uuid4(), uuid.uuid4())
        assert denied.status == "RATE_GLOBAL"
        assert denied.lease is None
        assert await redis_client.get(state_key) == before


@pytest.mark.parametrize("age_ms", [3_600_001, 3_600_000, 3_570_000])
async def test_rolling_window_cutoff_uses_redis_time_and_prunes_only_on_admission(
    age_ms: int,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    at_ms = seconds * 1000 + micros // 1000 - age_ms
    record = {"at_ms": at_ms, "user_id": _USER.hex, "repository_id": _REPOSITORY.hex}
    state = json.dumps({"schema_version": 2, "lease": None, "admissions": [record] * 3})
    await redis_client.set(state_key, state)
    result = await admissions[1].acquire(_USER, _REPOSITORY)
    if age_ms < _WINDOW_MS:
        # 30s slack avoids crossing the cutoff between preparation and EVAL.
        assert result.status == "RATE_USER"
        assert result.lease is None
        assert await redis_client.get(state_key) == state
    else:
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await admissions[0].release(result.lease.owner_token) == "RELEASED"
        raw = await redis_client.get(state_key)
        assert isinstance(raw, str)
        stored = json.loads(raw)
        assert stored["admissions"] == [
            {
                "at_ms": result.lease.expires_at_ms - 90_000,
                "user_id": _USER.hex,
                "repository_id": _REPOSITORY.hex,
            }
        ]
        assert stored["lease"] is None


async def test_previous_calendar_hour_admissions_still_count(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    now = seconds * 1000 + micros // 1000
    previous_hour = now // 3_600_000 * 3_600_000 - 1
    # At the very end of an hour this fixture has insufficient time to execute.
    if now - previous_hour > _WINDOW_MS - _BOUNDARY_SLACK_MS:
        pytest.skip("previous-hour fixture is within 30 seconds of expiry")
    record = {
        "at_ms": previous_hour,
        "user_id": _USER.hex,
        "repository_id": _REPOSITORY.hex,
    }
    state = json.dumps({"schema_version": 2, "lease": None, "admissions": [record] * 3})
    await redis_client.set(state_key, state)
    assert (await admissions[0].acquire(_USER, _REPOSITORY)).status == "RATE_USER"
    assert await redis_client.get(state_key) == state


@pytest.mark.parametrize(
    "quota,ceiling", [("USER", 3), ("REPOSITORY", 10), ("GLOBAL", 20)]
)
async def test_concurrent_pools_cannot_spend_last_allowance_twice(
    quota: str,
    ceiling: int,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    records = [
        {
            "at_ms": seconds * 1000 + micros // 1000,
            "user_id": (_USER if quota == "USER" else uuid.uuid4()).hex,
            "repository_id": (_REPOSITORY if quota != "GLOBAL" else uuid.uuid4()).hex,
        }
        for _ in range(ceiling - 1)
    ]
    await redis_client.set(
        state_key,
        json.dumps(
            {
                "schema_version": 2,
                "lease": None,
                "admissions": records,
            }
        ),
    )
    start = asyncio.Event()

    async def contend(index: int) -> AdmissionResult:
        await start.wait()
        client = admissions[index % 2]
        result = await client.acquire(
            _USER if quota == "USER" else uuid.uuid4(),
            _REPOSITORY if quota != "GLOBAL" else uuid.uuid4(),
        )
        if result.lease is not None:
            assert await client.release(result.lease.owner_token) == "RELEASED"
        return result

    tasks = [asyncio.create_task(contend(index)) for index in range(30)]
    start.set()
    results = await asyncio.gather(*tasks)
    assert sum(result.status == "ACQUIRED" for result in results) == 1
    assert all(
        result.status in {"ACQUIRED", "BUSY", f"RATE_{quota}"} for result in results
    )
    raw = await redis_client.get(state_key)
    assert isinstance(raw, str)
    stored = json.loads(raw)
    assert len(stored["admissions"]) == ceiling
    assert stored["lease"] is None
    assert len(raw.encode()) <= _MAX_STATE_BYTES
    # After release, the final denial is the quota, rather than concurrency.
    assert (await admissions[1].acquire(_USER, _REPOSITORY)).status == f"RATE_{quota}"


async def test_busy_attempts_do_not_spend_user_allowance(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    first, second = admissions
    owner = (await first.acquire(uuid.uuid4(), uuid.uuid4())).lease
    assert owner is not None
    before = await redis_client.get(state_key)
    for _ in range(4):
        assert (await second.acquire(_USER, _REPOSITORY)).status == "BUSY"
    assert await redis_client.get(state_key) == before
    assert await first.release(owner.owner_token) == "RELEASED"
    for _ in range(3):
        result = await second.acquire(_USER, _REPOSITORY)
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await second.release(result.lease.owner_token) == "RELEASED"
    assert (await first.acquire(_USER, _REPOSITORY)).status == "RATE_USER"


async def test_user_denials_do_not_spend_repository_or_global_allowance(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    for _ in range(3):
        result = await admissions[0].acquire(_USER, uuid.uuid4())
        assert result.lease is not None
        assert await admissions[0].release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    for _ in range(4):
        assert (await admissions[1].acquire(_USER, _REPOSITORY)).status == "RATE_USER"
    assert await redis_client.get(state_key) == before
    for index in range(17):
        result = await admissions[index % 2].acquire(
            uuid.uuid4(),
            _REPOSITORY if index < _REPOSITORY_LIMIT else uuid.uuid4(),
        )
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert (
            await admissions[index % 2].release(result.lease.owner_token) == "RELEASED"
        )
    assert (
        await admissions[1].acquire(uuid.uuid4(), uuid.uuid4())
    ).status == "RATE_GLOBAL"


@pytest.mark.parametrize(
    "quota,ceiling", [("USER", 3), ("REPOSITORY", 10), ("GLOBAL", 20)]
)
async def test_rate_denial_performs_no_write_even_of_identical_bytes(
    quota: str,
    ceiling: int,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    records = [
        {
            "at_ms": seconds * 1000 + micros // 1000,
            "user_id": (_USER if quota == "USER" else uuid.uuid4()).hex,
            "repository_id": (_REPOSITORY if quota != "GLOBAL" else uuid.uuid4()).hex,
        }
        for _ in range(ceiling)
    ]
    before = json.dumps({"schema_version": 2, "lease": None, "admissions": records})
    await redis_client.set(state_key, before)
    async with redis_client.pipeline() as watcher:
        await watcher.watch(state_key)
        for client in admissions:
            result = await client.acquire(_USER, _REPOSITORY)
            assert result.status == f"RATE_{quota}"
            assert result.lease is None
        watcher.multi()  # type: ignore[no-untyped-call]
        watcher.get(state_key)
        assert await watcher.execute() == [before]  # Any SET invalidates WATCH.


async def test_release_preserves_even_expired_history_without_pruning(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    now = seconds * 1000 + micros // 1000
    records = [
        {
            "at_ms": now - 3_600_001,
            "user_id": _USER.hex,
            "repository_id": _REPOSITORY.hex,
        }
    ]
    state = {
        "schema_version": 2,
        "lease": {"owner_token": "a" * 64, "expires_at_ms": now + 90_000},
        "admissions": records,
    }
    await redis_client.set(state_key, json.dumps(state))
    assert await admissions[1].release("a" * 64) == "RELEASED"
    raw = await redis_client.get(state_key)
    assert raw is not None
    assert json.loads(raw) == {**state, "lease": None}


async def test_denial_priority_user_then_repository_then_global(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    now = seconds * 1000 + micros // 1000
    records = [
        {
            "at_ms": now,
            "user_id": (_USER if i < _USER_LIMIT else uuid.uuid4()).hex,
            "repository_id": (
                _REPOSITORY if i < _REPOSITORY_LIMIT else uuid.uuid4()
            ).hex,
        }
        for i in range(20)
    ]
    state = json.dumps({"schema_version": 2, "lease": None, "admissions": records})
    await redis_client.set(state_key, state)
    assert (await admissions[0].acquire(_USER, _REPOSITORY)).status == "RATE_USER"
    assert (
        await admissions[1].acquire(uuid.uuid4(), _REPOSITORY)
    ).status == "RATE_REPOSITORY"
    assert (
        await admissions[0].acquire(uuid.uuid4(), uuid.uuid4())
    ).status == "RATE_GLOBAL"
    assert await redis_client.get(state_key) == state
    busy = json.dumps(
        {
            "schema_version": 2,
            "lease": {"owner_token": "b" * 64, "expires_at_ms": now + 90_000},
            "admissions": records,
        }
    )
    await redis_client.set(state_key, busy)
    assert (await admissions[1].acquire(_USER, _REPOSITORY)).status == "BUSY"
    assert await redis_client.get(state_key) == busy


_INVALID_ADMISSION_FIELDS: list[dict[str, object]] = [
    {"at_ms": None},
    {"at_ms": True},
    {"at_ms": "1"},
    {"at_ms": 0},
    {"at_ms": -1},
    {"at_ms": 1.5},
    {"at_ms": 9007199254740992},
    {"user_id": None},
    {"user_id": 5},
    {"user_id": "z" * 32},
    {"user_id": _USER.hex.upper()},
    {"user_id": str(_USER)},
    {"repository_id": False},
    {"repository_id": "a" * 31},
    {"repository_id": "g" * 32},
    {"extra": 'private[\\"]'},
]


@pytest.mark.parametrize(
    "history",
    [
        None,
        False,
        1,
        "[]",
        {},
        {"array": []},
        {"1": {}},
        [None],
        [[]],
        [{}],
        [{"at_ms": 1, "user_id": _USER.hex, "repository_id": _REPOSITORY.hex}] * 21,
        *[
            [
                {
                    "at_ms": 1,
                    "user_id": _USER.hex,
                    "repository_id": _REPOSITORY.hex,
                    **change,
                }
            ]
            for change in _INVALID_ADMISSION_FIELDS
        ],
    ],
)
@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_invalid_history_is_rejected_without_repair(
    history: object,
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    state = json.dumps(
        {
            "schema_version": 2,
            "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
            "admissions": history,
        }
    )
    await redis_client.set(state_key, state)
    if operation == "acquire":
        result = await admissions[0].acquire(_USER, _REPOSITORY)
        assert result.status == "INVALID_STATE"
        assert result.lease is None
    else:
        assert await admissions[1].release("a" * 64) == "INVALID_STATE"
    assert await redis_client.get(state_key) == state
    assert await redis_client.pttl(state_key) == -1


@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_v2_requires_history_array(
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    state = (
        '{"schema_version":2,"lease":{"owner_token":"'
        + "a" * 64
        + '","expires_at_ms":1}}'
    )
    await redis_client.set(state_key, state)
    if operation == "acquire":
        assert (
            await admissions[0].acquire(_USER, _REPOSITORY)
        ).status == "INVALID_STATE"
    else:
        assert await admissions[1].release("a" * 64) == "INVALID_STATE"
    assert await redis_client.get(state_key) == state


async def test_release_preserves_maximum_safe_integer_in_history(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    records = [
        {
            "at_ms": 9007199254740991,
            "user_id": _USER.hex,
            "repository_id": _REPOSITORY.hex,
        }
    ]
    state = {
        "schema_version": 2,
        "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
        "admissions": records,
    }
    await redis_client.set(state_key, json.dumps(state))
    assert await admissions[0].release("a" * 64) == "RELEASED"
    raw = await redis_client.get(state_key)
    assert raw is not None
    assert json.loads(raw) == {**state, "lease": None}
    # Valid history stays usable; future records count conservatively on rollback.
    assert (
        await admissions[1].acquire(uuid.uuid4(), uuid.uuid4())
    ).status == "ACQUIRED"
