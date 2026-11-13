"""Inspect effective redis-py connections, including conflicting URL options."""

from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from redis.backoff import NoBackoff

from app.adapters.simulation_admission import (
    RedisSimulationAdmission,
    create_admission_client,
)

_SOCKET_TIMEOUT_SECONDS = 0.5
_RESP2 = 2


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        "",
        "unexpected",
        1,
        False,
        b"RELEASED",
        ["RELEASED"],
        {"status": "NOT_OWNER"},
        ["INVALID_STATE", "extra"],
    ],
)
async def test_unknown_release_outcome_is_unavailable(
    response: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def eval_response(*args: object, **kwargs: object) -> object:
        return response

    monkeypatch.setattr(Redis, "eval", eval_response)
    admission = RedisSimulationAdmission(
        "redis://127.0.0.1:1/0", hmac_secret=b"test-only-secret-material-32bytes!"
    )
    try:
        assert await admission.release("a" * 64) == "UNAVAILABLE"
    finally:
        await admission.aclose()


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?socket_timeout=999&socket_connect_timeout=888&retry_on_timeout=true"
        "&retry=bad&protocol=3&decode_responses=false",
    ],
)
async def test_admission_pool_enforces_no_retries_and_short_timeouts(
    query: str,
) -> None:
    client = create_admission_client(f"redis://127.0.0.1:1/0{query}")
    try:
        # Make the same connection object redis-py will use; no network required.
        connection = client.connection_pool.make_connection()  # type: ignore[no-untyped-call]
        assert connection.retry.get_retries() == 0
        assert isinstance(connection.retry._backoff, NoBackoff)
        assert not connection.retry_on_error
        assert connection.socket_connect_timeout == _SOCKET_TIMEOUT_SECONDS
        assert connection.socket_timeout == _SOCKET_TIMEOUT_SECONDS
        assert connection.protocol == _RESP2
        assert connection.encoder.decode_responses is True
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        ["unexpected"],
        ["ACQUIRED"],
        ["ACQUIRED", "NaN"],
        ["ACQUIRED", "-1"],
        ["ACQUIRED", "9007199254740992"],
        ["ACQUIRED", "123", "extra"],
        ["BUSY", "extra"],
    ],
)
async def test_ambiguous_redis_response_never_grants_a_lease(
    response: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def eval_response(*args: object, **kwargs: object) -> object:
        return response

    # Fault injection at the external Redis transport boundary, no real socket.
    monkeypatch.setattr(Redis, "eval", eval_response)
    admission = RedisSimulationAdmission(
        "redis://127.0.0.1:1/0", hmac_secret=b"test-only-secret-material-32bytes!"
    )
    try:
        result = await admission.acquire(uuid4(), uuid4(), uuid4().bytes)
        assert result.status == "UNAVAILABLE"
        assert result.lease is None
    finally:
        await admission.aclose()


@pytest.mark.parametrize(
    "secret", [None, "invalid-secret", b"", b"x" * 31, b"x" * 1025, 1]
)
async def test_missing_or_invalid_secret_denies_without_redis_eval(
    secret: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    evaluated = False

    async def eval_response(*args: object, **kwargs: object) -> object:
        nonlocal evaluated
        evaluated = True
        return ["ACQUIRED", "123"]

    monkeypatch.setattr(Redis, "eval", eval_response)
    admission = RedisSimulationAdmission(
        "redis://127.0.0.1:1/0",
        hmac_secret=secret,  # type: ignore[arg-type]
    )
    try:
        result = await admission.acquire(uuid4(), uuid4(), b"private-key")
        assert result.status == "UNAVAILABLE"
        assert result.lease is None
        assert await admission.release("a" * 64) == "UNAVAILABLE"
        assert not evaluated
        assert admission.hmac_config_id is None
        assert "invalid-secret" not in caplog.text
        assert "invalid-secret" not in repr(admission)
    finally:
        await admission.aclose()


async def test_eval_receives_only_domain_separated_hmac_and_safe_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: tuple[object, ...] = ()

    async def eval_response(*args: object, **kwargs: object) -> object:
        nonlocal received
        received = args
        return ["DUPLICATE"]

    monkeypatch.setattr(Redis, "eval", eval_response)
    secret = b"test-only-secret-material-32bytes!"
    admission = RedisSimulationAdmission("redis://127.0.0.1:1/0", hmac_secret=secret)
    try:
        result = await admission.acquire(
            UUID("00112233-4455-6677-8899-aabbccddeeff"),
            UUID("ffeeddcc-bbaa-9988-7766-554433221100"),
            b"key\x00\xff",
        )
        assert result.status == "DUPLICATE"
        assert result.lease is None
        # Fixed known vector for the documented 8-byte length-prefixed wire format.
        assert received[-2:] == (
            "ca6e7e997beb8c3d39507959a82b87bced5af603a4304520759b1179f4fbf140",
            "8293be9a6ecf5eb02454c7fcca21cf388379450a6c5f44051c0fbf391b5c7f37",
        )
        assert admission.hmac_config_id == received[-1]
        assert secret not in received
        assert secret.decode() not in repr(received)
        assert b"key\x00\xff" not in received
    finally:
        await admission.aclose()
