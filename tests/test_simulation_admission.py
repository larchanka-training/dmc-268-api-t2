"""Public admission seam against an explicitly disposable Redis server."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
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
_MAX_STATE_BYTES = 65536
_CONFIG_ID = "8293be9a6ecf5eb02454c7fcca21cf388379450a6c5f44051c0fbf391b5c7f37"
_GLOBAL_LIMIT = 20
_MAX_CONSUMED_KEYS = 480
_USER_LIMIT = 3
_REPOSITORY_LIMIT = 10
_OWNER_TOKEN_HEX_LENGTH = 64
_PHYSICAL_TTL_MS = 60_000
_SECOND_CONNECTION = 2
_TWO_ADMISSIONS = 2
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
    await redis_client.set(
        key,
        json.dumps(
            {
                "schema_version": 3,
                "hmac_config_id": _CONFIG_ID,
                "consumed_keys": [],
                "lease": None,
                "admissions": [],
            }
        ),
    )
    try:
        yield key
    finally:
        await redis_client.delete(key)


@pytest.fixture
async def admissions(
    state_key: str,
) -> AsyncIterator[tuple[RedisSimulationAdmission, RedisSimulationAdmission]]:
    assert _REDIS_URL is not None
    first = RedisSimulationAdmission(
        _REDIS_URL,
        hmac_secret=b"test-only-secret-material-32bytes!",
        state_key=state_key,
    )
    second = RedisSimulationAdmission(
        _REDIS_URL,
        hmac_secret=b"test-only-secret-material-32bytes!",
        state_key=state_key,
    )
    try:
        yield first, second
    finally:
        await first.aclose()
        await second.aclose()


async def _resp_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one RESP2 frame without changing the real Redis reply."""
    line = await reader.readuntil(b"\r\n")
    if line[:1] == b"$" and int(line[1:-2]) >= 0:
        return line + await reader.readexactly(int(line[1:-2]) + 2)
    if line[:1] == b"*" and int(line[1:-2]) >= 0:
        parts = [await _resp_frame(reader) for _ in range(int(line[1:-2]))]
        return line + b"".join(parts)
    assert line[:1] in (b"+", b"-", b":", b"$", b"*")
    return line


@dataclass
class _ReplyGate:
    mode: str | None = None
    committed: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    eval_connections: list[int] = field(default_factory=list)
    connections: int = 0


@pytest.fixture
async def gated_admission(  # noqa: PLR0915 -- real socket proxy lifecycle and cleanup
    state_key: str,
) -> AsyncIterator[tuple[RedisSimulationAdmission, _ReplyGate]]:
    """Real Redis behind a proxy that interrupts exactly one EVAL reply.

    The complete upstream reply proves EVAL finished. Only a partial RESP frame
    reaches redis-py, exercising its actual socket reader, timeout and cleanup.
    No Redis method or connection implementation is replaced.
    """
    assert _REDIS_URL is not None
    parts = urlsplit(_REDIS_URL)
    gate = _ReplyGate()
    handlers: list[asyncio.Task[None]] = []

    async def forward(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.append(task)
        gate.connections += 1
        connection_id = gate.connections
        upstream_writer = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                parts.hostname, parts.port or 6379
            )
            while True:
                command = await _resp_frame(reader)
                is_eval = command.split(b"\r\n", 3)[2] == b"EVAL"
                if is_eval:
                    gate.eval_connections.append(connection_id)
                upstream_writer.write(command)
                await upstream_writer.drain()
                response = await _resp_frame(upstream_reader)
                if is_eval and gate.mode is not None:
                    mode, gate.mode = gate.mode, None
                    writer.write(response[:3])  # Interrupt inside a RESP frame.
                    await writer.drain()
                    gate.committed.set()
                    if mode != "disconnect":
                        # Timeout/cancellation must close the client socket.
                        assert await reader.read() == b""
                    gate.disconnected.set()
                    return
                writer.write(response)
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionResetError:
            pass
        finally:
            for stream in (writer, upstream_writer):
                if stream is not None:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()

    server = await asyncio.start_server(forward, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    credentials = parts.netloc.rsplit("@", 1)[0] + "@" if parts.username else ""
    url = urlunsplit(parts._replace(netloc=f"{credentials}127.0.0.1:{port}"))
    admission = RedisSimulationAdmission(
        url, hmac_secret=b"test-only-secret-material-32bytes!", state_key=state_key
    )
    # A second command must reuse this pool's only connection object safely.
    admission._client.connection_pool.max_connections = 1
    try:
        yield admission, gate
    finally:
        await admission.aclose()
        server.close()
        await server.wait_closed()
        for handler in handlers:
            if not handler.done():
                handler.cancel()
        outcomes = await asyncio.gather(*handlers, return_exceptions=True)
        assert all(
            outcome is None or isinstance(outcome, asyncio.CancelledError)
            for outcome in outcomes
        ), outcomes


@pytest.mark.parametrize("loss", ["disconnect", "timeout"])
async def test_acquire_committed_but_reply_lost_denies_without_retry_or_refund(
    loss: str,
    redis_client: Redis,
    state_key: str,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
) -> None:
    admission, gate = gated_admission
    gate.mode = loss
    result = await asyncio.wait_for(
        admission.acquire(_USER, _REPOSITORY, b"lost-acquire"), 3
    )
    assert gate.committed.is_set()
    await asyncio.wait_for(gate.disconnected.wait(), 3)
    assert result.status == "UNAVAILABLE"
    assert result.lease is None
    assert len(gate.eval_connections) == 1
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    assert state["lease"] is not None
    assert len(state["admissions"]) == len(state["consumed_keys"]) == 1
    assert state["consumed_keys"][0]["at_ms"] == state["admissions"][0]["at_ms"]
    # Fresh ECHO must not receive the interrupted EVAL's residual RESP frame.
    assert await admission._client.echo("fresh-after-acquire") == "fresh-after-acquire"
    assert gate.connections == _SECOND_CONNECTION
    duplicate = await admission.acquire(_USER, _REPOSITORY, b"lost-acquire")
    assert duplicate.status == "DUPLICATE"
    assert duplicate.lease is None
    assert gate.eval_connections == [1, 2]  # Second EVAL is this explicit call.
    assert await redis_client.get(state_key) == raw


@pytest.mark.parametrize("loss", ["disconnect", "timeout"])
async def test_release_committed_but_reply_lost_preserves_history_and_new_owner(
    loss: str,
    redis_client: Redis,
    state_key: str,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
) -> None:
    admission, gate = gated_admission
    old = await admission.acquire(_USER, _REPOSITORY, b"lost-release")
    assert old.lease is not None
    before = await redis_client.get(state_key)
    assert before is not None
    gate.mode = loss
    result = await asyncio.wait_for(admission.release(old.lease.owner_token), 3)
    assert gate.committed.is_set()
    await asyncio.wait_for(gate.disconnected.wait(), 3)
    assert result == "UNAVAILABLE"
    assert gate.eval_connections == [1, 1]  # Acquire, then exactly one release.
    released = await redis_client.get(state_key)
    assert released is not None
    assert json.loads(released) == {**json.loads(before), "lease": None}
    assert await admission._client.echo("fresh-after-release") == "fresh-after-release"
    assert gate.connections == _SECOND_CONNECTION
    assert (
        await admission.acquire(_USER, _REPOSITORY, b"lost-release")
    ).status == "DUPLICATE"
    assert await redis_client.get(state_key) == released
    new = await admission.acquire(_USER, _REPOSITORY, b"new-owner")
    assert new.lease is not None
    assert new.lease.owner_token != old.lease.owner_token
    new_state = await redis_client.get(state_key)
    assert new_state is not None
    assert len(json.loads(new_state)["admissions"]) == _TWO_ADMISSIONS
    assert len(json.loads(new_state)["consumed_keys"]) == _TWO_ADMISSIONS
    assert await admission.release(old.lease.owner_token) == "NOT_OWNER"
    assert await redis_client.get(state_key) == new_state
    assert await admission.release(new.lease.owner_token) == "RELEASED"
    assert gate.eval_connections == [1, 1, 2, 2, 2, 2]


@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_cancellation_during_redis_reply_propagates_and_discards_connection(
    operation: str,
    redis_client: Redis,
    state_key: str,
    gated_admission: tuple[RedisSimulationAdmission, _ReplyGate],
) -> None:
    admission, gate = gated_admission
    owner = None
    before = None
    if operation == "release":
        acquired = await admission.acquire(_USER, _REPOSITORY, b"cancelled-operation")
        assert acquired.lease is not None
        owner = acquired.lease.owner_token
        before = await redis_client.get(state_key)
        assert before is not None
    prior_evals = len(gate.eval_connections)
    gate.mode = "cancellation"
    task = asyncio.create_task(
        admission.release(owner)
        if owner is not None
        else admission.acquire(_USER, _REPOSITORY, b"cancelled-operation")
    )
    try:
        await asyncio.wait_for(gate.committed.wait(), 3)
        # EVAL finished upstream; redis-py is still reading a partial reply.
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(gate.disconnected.wait(), 3)
        assert len(gate.eval_connections) == prior_evals + 1
        raw = await redis_client.get(state_key)
        assert raw is not None
        state = json.loads(raw)
        assert len(state["admissions"]) == len(state["consumed_keys"]) == 1
        if before is not None:
            assert state == {**json.loads(before), "lease": None}
        else:
            assert state["lease"] is not None
        assert (
            await admission._client.echo("fresh-after-cancel") == "fresh-after-cancel"
        )
        assert gate.connections == _SECOND_CONNECTION
        duplicate = await admission.acquire(_USER, _REPOSITORY, b"cancelled-operation")
        assert duplicate.status == "DUPLICATE"
        assert duplicate.lease is None
        assert len(gate.eval_connections) == prior_evals + 2
        assert gate.eval_connections[-1] == _SECOND_CONNECTION
        assert await redis_client.get(state_key) == raw
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
        return await client.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)

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
    state = json.loads(stored)
    consumed = state.pop("consumed_keys")
    assert len(consumed) == 1
    assert set(consumed[0]) == {"digest", "at_ms"}
    assert consumed[0]["at_ms"] == winner.expires_at_ms - 90_000
    assert state == {
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
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
    assert (
        await admissions[0].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
    ).status == "BUSY"
    assert (
        await admissions[1].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
    ).status == "BUSY"
    assert await redis_client.get(state_key) == stored


async def test_redis_error_is_unavailable_never_admission() -> None:
    admission = RedisSimulationAdmission(
        "redis://127.0.0.1:1/0", hmac_secret=b"test-only-secret-material-32bytes!"
    )
    try:
        result = await admission.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
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
    old = (await first.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)).lease
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
    new = (await second.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)).lease
    assert new is not None and new.owner_token != old.owner_token
    new_state = await redis_client.get(state_key)
    assert await first.release(old.owner_token) == "NOT_OWNER"
    assert await redis_client.get(state_key) == new_state
    assert await second.release(new.owner_token) == "RELEASED"
    assert await redis_client.pttl(state_key) == -1


async def test_release_redis_error_is_unavailable() -> None:
    admission = RedisSimulationAdmission(
        "redis://127.0.0.1:1/0", hmac_secret=b"test-only-secret-material-32bytes!"
    )
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
                "schema_version": 3,
                "hmac_config_id": _CONFIG_ID,
                "consumed_keys": [],
                "admissions": [],
                "lease": {
                    "owner_token": "a" * 64,
                    "expires_at_ms": seconds * 1000 + micros // 1000 - 1,
                },
            }
        ),
    )
    result = await admissions[1].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
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
        pytest.param(
            json.dumps(
                {
                    "schema_version": 3,
                    "hmac_config_id": _CONFIG_ID,
                    "consumed_keys": [],
                    "admissions": [],
                }
            ),
            id="missing-lease",
        ),
        pytest.param(
            json.dumps(
                {
                    "schema_version": 3,
                    "hmac_config_id": _CONFIG_ID,
                    "consumed_keys": [],
                    "lease": False,
                    "admissions": [],
                }
            ),
            id="lease-type",
        ),
        pytest.param(
            json.dumps(
                {
                    "schema_version": 3,
                    "hmac_config_id": _CONFIG_ID,
                    "consumed_keys": [],
                    "lease": {},
                    "admissions": [],
                }
            ),
            id="lease-fields",
        ),
        pytest.param(
            json.dumps(
                {
                    "schema_version": 3,
                    "hmac_config_id": _CONFIG_ID,
                    "consumed_keys": [],
                    "lease": None,
                    "admissions": [],
                    "extra": "private",
                }
            ),
            id="extra-field",
        ),
        pytest.param(
            " " * _MAX_STATE_BYTES
            + json.dumps(
                {
                    "schema_version": 3,
                    "hmac_config_id": _CONFIG_ID,
                    "consumed_keys": [],
                    "lease": None,
                    "admissions": [],
                }
            ),
            id="oversize",
        ),
        *[
            pytest.param(
                json.dumps(
                    {
                        "schema_version": 3,
                        "hmac_config_id": _CONFIG_ID,
                        "consumed_keys": [],
                        "admissions": [],
                        "lease": lease,
                    }
                ),
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
        result = await admissions[0].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
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
    lease = (await admissions[0].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)).lease
    assert lease is not None
    await redis_client.pexpire(state_key, _PHYSICAL_TTL_MS)
    before = await redis_client.get(state_key)
    if operation == "acquire":
        assert (
            await admissions[1].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
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
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "consumed_keys": [],
            "admissions": [],
            "lease": {
                "owner_token": "b" * 64,
                "expires_at_ms": seconds * 1000 + micros // 1000 + 90_000,
            },
        }
    )
    await redis_client.set(state_key, state)
    results = await asyncio.gather(
        *(
            client.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
            for client in admissions
        )
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
                "schema_version": 3,
                "hmac_config_id": _CONFIG_ID,
                "consumed_keys": [],
                "admissions": [],
                "lease": {
                    "owner_token": "c" * 64,
                    "expires_at_ms": seconds * 1000 + micros // 1000 - 1,
                },
            }
        ),
    )
    results = await asyncio.gather(
        *(
            client.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
            for client in admissions
        )
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
        lease = (
            await admissions[0].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
        ).lease
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
    denied = RedisSimulationAdmission(
        url, hmac_secret=b"test-only-secret-material-32bytes!", state_key=state_key
    )
    try:
        if operation == "acquire":
            result = await denied.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
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
        result = await client.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await client.release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    denied = await admissions[1].acquire(_USER, uuid.uuid4(), uuid.uuid4().bytes)
    assert denied.status == "RATE_USER"
    assert denied.lease is None
    assert await redis_client.get(state_key) == before
    # Denial did not create a lease or consume another user's allowance.
    other = await admissions[0].acquire(uuid.uuid4(), _REPOSITORY, uuid.uuid4().bytes)
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
        result = await client.acquire(uuid.uuid4(), _REPOSITORY, uuid.uuid4().bytes)
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await client.release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    denied_user = uuid.uuid4()
    denied = await admissions[1].acquire(denied_user, _REPOSITORY, uuid.uuid4().bytes)
    assert denied.status == "RATE_REPOSITORY"
    assert denied.lease is None
    assert await redis_client.get(state_key) == before
    # Same user can still spend all three admissions on another repository.
    for client in (admissions[0], admissions[1], admissions[0]):
        result = await client.acquire(denied_user, uuid.uuid4(), uuid.uuid4().bytes)
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
        result = await client.acquire(uuid.uuid4(), uuid.uuid4(), uuid.uuid4().bytes)
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await client.release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    for client in reversed(admissions):
        denied = await client.acquire(uuid.uuid4(), uuid.uuid4(), uuid.uuid4().bytes)
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
    state = json.dumps(
        {
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "consumed_keys": [],
            "lease": None,
            "admissions": [record] * 3,
        }
    )
    await redis_client.set(state_key, state)
    result = await admissions[1].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
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
    state = json.dumps(
        {
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "consumed_keys": [],
            "lease": None,
            "admissions": [record] * 3,
        }
    )
    await redis_client.set(state_key, state)
    assert (
        await admissions[0].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
    ).status == "RATE_USER"
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
                "schema_version": 3,
                "hmac_config_id": _CONFIG_ID,
                "consumed_keys": [],
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
            uuid.uuid4().bytes,
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
    assert isinstance(raw, str)
    assert len(raw.encode()) <= _MAX_STATE_BYTES
    # After release, the final denial is the quota, rather than concurrency.
    assert (
        await admissions[1].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
    ).status == f"RATE_{quota}"


async def test_busy_attempts_do_not_spend_user_allowance(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    first, second = admissions
    owner = (await first.acquire(uuid.uuid4(), uuid.uuid4(), uuid.uuid4().bytes)).lease
    assert owner is not None
    before = await redis_client.get(state_key)
    for _ in range(4):
        assert (
            await second.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
        ).status == "BUSY"
    assert await redis_client.get(state_key) == before
    assert await first.release(owner.owner_token) == "RELEASED"
    for _ in range(3):
        result = await second.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await second.release(result.lease.owner_token) == "RELEASED"
    assert (
        await first.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
    ).status == "RATE_USER"


async def test_user_denials_do_not_spend_repository_or_global_allowance(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    for _ in range(3):
        result = await admissions[0].acquire(_USER, uuid.uuid4(), uuid.uuid4().bytes)
        assert result.lease is not None
        assert await admissions[0].release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    for _ in range(4):
        assert (
            await admissions[1].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
        ).status == "RATE_USER"
    assert await redis_client.get(state_key) == before
    for index in range(17):
        result = await admissions[index % 2].acquire(
            uuid.uuid4(),
            _REPOSITORY if index < _REPOSITORY_LIMIT else uuid.uuid4(),
            uuid.uuid4().bytes,
        )
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert (
            await admissions[index % 2].release(result.lease.owner_token) == "RELEASED"
        )
    assert (
        await admissions[1].acquire(uuid.uuid4(), uuid.uuid4(), uuid.uuid4().bytes)
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
    before = json.dumps(
        {
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "consumed_keys": [],
            "lease": None,
            "admissions": records,
        }
    )
    await redis_client.set(state_key, before)
    async with redis_client.pipeline() as watcher:
        await watcher.watch(state_key)
        for client in admissions:
            result = await client.acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
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
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
        "consumed_keys": [],
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
    state = json.dumps(
        {
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "consumed_keys": [],
            "lease": None,
            "admissions": records,
        }
    )
    await redis_client.set(state_key, state)
    assert (
        await admissions[0].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
    ).status == "RATE_USER"
    assert (
        await admissions[1].acquire(uuid.uuid4(), _REPOSITORY, uuid.uuid4().bytes)
    ).status == "RATE_REPOSITORY"
    assert (
        await admissions[0].acquire(uuid.uuid4(), uuid.uuid4(), uuid.uuid4().bytes)
    ).status == "RATE_GLOBAL"
    assert await redis_client.get(state_key) == state
    busy = json.dumps(
        {
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "consumed_keys": [],
            "lease": {"owner_token": "b" * 64, "expires_at_ms": now + 90_000},
            "admissions": records,
        }
    )
    await redis_client.set(state_key, busy)
    assert (
        await admissions[1].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
    ).status == "BUSY"
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
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "consumed_keys": [],
            "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
            "admissions": history,
        }
    )
    await redis_client.set(state_key, state)
    if operation == "acquire":
        result = await admissions[0].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
        assert result.status == "INVALID_STATE"
        assert result.lease is None
    else:
        assert await admissions[1].release("a" * 64) == "INVALID_STATE"
    assert await redis_client.get(state_key) == state
    assert await redis_client.pttl(state_key) == -1


@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_v3_requires_history_array(
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    state = json.dumps(
        {
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "consumed_keys": [],
            "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
        }
    )
    await redis_client.set(state_key, state)
    if operation == "acquire":
        assert (
            await admissions[0].acquire(_USER, _REPOSITORY, uuid.uuid4().bytes)
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
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
        "consumed_keys": [],
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
        await admissions[1].acquire(uuid.uuid4(), uuid.uuid4(), uuid.uuid4().bytes)
    ).status == "ACQUIRED"


async def test_consumed_key_after_release_is_duplicate_without_writes_or_allowance(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    first, second = admissions
    key = b"private-idempotency-key"
    result = await first.acquire(_USER, _REPOSITORY, key)
    assert result.status == "ACQUIRED"
    assert result.lease is not None
    assert await second.release(result.lease.owner_token) == "RELEASED"
    before = await redis_client.get(state_key)
    async with redis_client.pipeline() as watcher:
        await watcher.watch(state_key)
        for _ in range(4):
            duplicate = await second.acquire(_USER, _REPOSITORY, key)
            assert duplicate.status == "DUPLICATE"
            assert duplicate.lease is None
        watcher.multi()  # type: ignore[no-untyped-call]
        watcher.get(state_key)
        assert await watcher.execute() == [before]
    assert before is not None
    stored = json.loads(before)
    assert stored["lease"] is None
    assert len(stored["admissions"]) == 1
    assert len(stored["consumed_keys"]) == 1
    assert key.decode() not in before
    assert "test-only-secret-material-32bytes!" not in before
    for next_key in (b"another-key", b"third-key"):
        allowed = await first.acquire(_USER, _REPOSITORY, next_key)
        assert allowed.status == "ACQUIRED"
        assert allowed.lease is not None
        assert await first.release(allowed.lease.owner_token) == "RELEASED"


async def test_scopes_and_exact_key_bytes_are_isolated(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    # No body parameter exists at this seam: changing body cannot change identity.
    key = b"same-key\x00\xff"
    scopes = [(_USER, _REPOSITORY), (uuid.uuid4(), _REPOSITORY), (_USER, uuid.uuid4())]
    for index, (user, repository) in enumerate(scopes):
        client = admissions[index % 2]
        result = await client.acquire(user, repository, key)
        assert result.status == "ACQUIRED"
        assert result.lease is not None
        assert await client.release(result.lease.owner_token) == "RELEASED"
        assert (await client.acquire(user, repository, key)).status == "DUPLICATE"
    # Key bytes are neither normalized nor truncated at NUL/non-ASCII bytes.
    result = await admissions[1].acquire(_USER, _REPOSITORY, b"same-key")
    assert result.status == "ACQUIRED"
    raw = await redis_client.get(state_key)
    assert raw is not None
    records = json.loads(raw)["consumed_keys"]
    assert len({record["digest"] for record in records}) == len(scopes) + 1
    assert all(set(record) == {"digest", "at_ms"} for record in records)


@pytest.mark.parametrize("_round", range(20))
async def test_same_key_concurrency_consumes_once_before_busy(
    _round: int,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    start = asyncio.Event()

    async def contend(client: RedisSimulationAdmission) -> AdmissionResult:
        await start.wait()
        return await client.acquire(_USER, _REPOSITORY, b"concurrent-key")

    tasks = [asyncio.create_task(contend(client)) for client in admissions]
    start.set()
    results = await asyncio.gather(*tasks)
    assert sorted(result.status for result in results) == ["ACQUIRED", "DUPLICATE"]
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    assert len(state["consumed_keys"]) == len(state["admissions"]) == 1
    assert state["consumed_keys"][0]["at_ms"] == state["admissions"][0]["at_ms"]
    winner = next(result.lease for result in results if result.lease is not None)
    assert state["lease"]["owner_token"] == winner.owner_token
    assert (await admissions[0].acquire(_USER, _REPOSITORY, b"other")).status == "BUSY"
    assert await redis_client.get(state_key) == raw


@pytest.mark.parametrize("age_ms", [86_400_000, 86_400_001])
async def test_consumed_key_expires_at_24_hour_cutoff_and_prunes_on_success(
    age_ms: int,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    key = b"24-hour-key"
    first = await admissions[0].acquire(_USER, _REPOSITORY, key)
    assert first.lease is not None
    assert await admissions[0].release(first.lease.owner_token) == "RELEASED"
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    seconds, micros = await redis_client.time()
    now = seconds * 1000 + micros // 1000
    state["admissions"] = []
    state["consumed_keys"][0]["at_ms"] = now - age_ms
    retained = {"digest": "f" * 64, "at_ms": now}
    state["consumed_keys"].extend([retained, {"digest": "e" * 64, "at_ms": 1}])
    await redis_client.set(state_key, json.dumps(state))
    result = await admissions[1].acquire(_USER, _REPOSITORY, key)
    assert result.status == "ACQUIRED"
    assert result.lease is not None
    raw = await redis_client.get(state_key)
    assert raw is not None
    consumed = json.loads(raw)["consumed_keys"]
    assert consumed == [
        retained,
        {
            "digest": state["consumed_keys"][0]["digest"],
            "at_ms": result.lease.expires_at_ms - 90_000,
        },
    ]


@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_more_than_480_consumed_records_is_invalid_without_repair(
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    state = {
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
        "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
        "admissions": [],
        "consumed_keys": [{"digest": f"{i:064x}", "at_ms": 1} for i in range(481)],
    }
    before = json.dumps(state)
    assert len(before.encode()) < _MAX_STATE_BYTES
    await redis_client.set(state_key, before)
    if operation == "acquire":
        result = await admissions[0].acquire(_USER, _REPOSITORY, b"capacity")
        assert result.status == "INVALID_STATE"
        assert result.lease is None
    else:
        assert await admissions[1].release("a" * 64) == "INVALID_STATE"
    assert await redis_client.get(state_key) == before


async def test_full_consumed_history_never_evicts_unexpired_keys(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    state = {
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
        "lease": None,
        "admissions": [],
        "consumed_keys": [
            {"digest": f"{i:064x}", "at_ms": 9007199254740991} for i in range(480)
        ],
    }
    # Future entries model clock rollback; all must be retained conservatively.
    before = json.dumps(state)
    await redis_client.set(state_key, before)
    result = await admissions[0].acquire(_USER, _REPOSITORY, b"no-eviction")
    assert result.status == "INVALID_STATE"
    assert result.lease is None
    assert await redis_client.get(state_key) == before


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
        [{"digest": "a" * 64}],
        [{"at_ms": 1}],
        [{"digest": "a" * 64, "at_ms": 1}] * 2,
        *[
            [{"digest": digest, "at_ms": 1}]
            for digest in (
                None,
                False,
                1,
                "",
                "a" * 63,
                "a" * 65,
                "A" * 64,
                "z" * 64,
                '"' * 64,
            )
        ],
        *[
            [{"digest": "a" * 64, "at_ms": stamp}]
            for stamp in (None, True, "1", 0, -1, 1.5, 9007199254740992)
        ],
        [{"digest": "a" * 64, "at_ms": 1, "extra": "private"}],
    ],
)
@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_corrupt_consumed_history_fails_closed_without_writes(
    history: object,
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    before = json.dumps(
        {
            "schema_version": 3,
            "hmac_config_id": _CONFIG_ID,
            "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
            "admissions": [],
            "consumed_keys": history,
        }
    )
    await redis_client.set(state_key, before)
    async with redis_client.pipeline() as watcher:
        await watcher.watch(state_key)
        if operation == "acquire":
            result = await admissions[0].acquire(_USER, _REPOSITORY, b"malformed")
            assert result.status == "INVALID_STATE"
            assert result.lease is None
        else:
            assert await admissions[1].release("a" * 64) == "INVALID_STATE"
        watcher.multi()  # type: ignore[no-untyped-call]
        watcher.get(state_key)
        assert await watcher.execute() == [before]


@pytest.mark.parametrize("config_id", [None, 1, "", "a" * 63, "a" * 64, "Z" * 64])
@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_incompatible_hmac_configuration_is_invalid_without_writes(
    config_id: object,
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    before = json.dumps(
        {
            "schema_version": 3,
            "hmac_config_id": config_id,
            "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
            "admissions": [],
            "consumed_keys": [],
        }
    )
    await redis_client.set(state_key, before)
    if operation == "acquire":
        result = await admissions[0].acquire(_USER, _REPOSITORY, b"config-mismatch")
        assert result.status == "INVALID_STATE"
        assert result.lease is None
    else:
        assert await admissions[1].release("a" * 64) == "INVALID_STATE"
    assert await redis_client.get(state_key) == before


@pytest.mark.parametrize(
    "outcome", ["DUPLICATE", "BUSY", "RATE_USER", "RATE_REPOSITORY", "RATE_GLOBAL"]
)
async def test_denials_never_prune_consumed_history_or_write(
    outcome: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    key = b"denial-history"
    result = await admissions[0].acquire(_USER, _REPOSITORY, key)
    assert result.lease is not None
    assert await admissions[0].release(result.lease.owner_token) == "RELEASED"
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    now = result.lease.expires_at_ms - 90_000
    # Same key is still consumed 30s before 24h; an expired key is also retained
    # until a successful admission. Denials do not even rewrite identical bytes.
    state["consumed_keys"][0]["at_ms"] = now - 86_370_000
    state["consumed_keys"].append({"digest": "f" * 64, "at_ms": 1})
    ceilings = {
        "DUPLICATE": 20,
        "BUSY": 20,
        "RATE_USER": 3,
        "RATE_REPOSITORY": 10,
        "RATE_GLOBAL": 20,
    }
    state["admissions"] = [
        {
            "at_ms": now,
            "user_id": (
                _USER if outcome in {"DUPLICATE", "BUSY", "RATE_USER"} else uuid.uuid4()
            ).hex,
            "repository_id": (
                _REPOSITORY if outcome != "RATE_GLOBAL" else uuid.uuid4()
            ).hex,
        }
        for _ in range(ceilings[outcome])
    ]
    if outcome in {"DUPLICATE", "BUSY"}:
        state["lease"] = {"owner_token": "a" * 64, "expires_at_ms": now + 90_000}
    before = json.dumps(state)
    await redis_client.set(state_key, before)
    async with redis_client.pipeline() as watcher:
        await watcher.watch(state_key)
        denied = await admissions[1].acquire(
            _USER, _REPOSITORY, key if outcome == "DUPLICATE" else b"new-key"
        )
        assert denied.status == outcome
        assert denied.lease is None
        watcher.multi()  # type: ignore[no-untyped-call]
        watcher.get(state_key)
        assert await watcher.execute() == [before]


@pytest.mark.parametrize("failure", ["timeout", "cancellation"])
async def test_caller_failure_never_unconsumes_acknowledged_admission(
    failure: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    ready = asyncio.Event()

    async def caller() -> None:
        result = await admissions[0].acquire(_USER, _REPOSITORY, b"failed-caller")
        assert result.lease is not None
        try:
            ready.set()
            if failure == "timeout":
                async with asyncio.timeout(0):
                    await asyncio.Event().wait()
            else:
                await asyncio.Event().wait()
        finally:
            assert await admissions[0].release(result.lease.owner_token) == "RELEASED"

    task = asyncio.create_task(caller())
    await ready.wait()
    if failure == "cancellation":
        task.cancel()
    with pytest.raises(
        TimeoutError if failure == "timeout" else asyncio.CancelledError
    ):
        await task
    result = await admissions[1].acquire(_USER, _REPOSITORY, b"failed-caller")
    assert result.status == "DUPLICATE"
    assert result.lease is None


async def test_maximum_24h_history_prunes_expired_and_retains_480_records(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    seconds, micros = await redis_client.time()
    now = seconds * 1000 + micros // 1000
    records = [
        {"digest": f"{i:064x}", "at_ms": now - ((i + 1) // 20) * _WINDOW_MS}
        for i in range(479)
    ]
    expired = {"digest": "f" * 64, "at_ms": now - 86_400_000}
    state = {
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
        "lease": None,
        "consumed_keys": [expired, *records],
        "admissions": [
            {
                "at_ms": now,
                "user_id": uuid.uuid4().hex,
                "repository_id": uuid.uuid4().hex,
            }
            for _ in range(19)
        ],
    }
    await redis_client.set(state_key, json.dumps(state))
    result = await admissions[0].acquire(_USER, _REPOSITORY, b"480th-key")
    assert result.status == "ACQUIRED"
    assert result.lease is not None
    assert await admissions[1].release(result.lease.owner_token) == "RELEASED"
    raw = await redis_client.get(state_key)
    assert raw is not None
    stored = json.loads(raw)
    assert stored["consumed_keys"][:-1] == records
    assert len(stored["consumed_keys"]) == _MAX_CONSUMED_KEYS
    assert len(stored["admissions"]) == _GLOBAL_LIMIT
    assert isinstance(raw, str)
    assert len(raw.encode()) <= _MAX_STATE_BYTES
    assert await redis_client.pttl(state_key) == -1


async def test_release_and_acquire_preserve_large_consumed_timestamps(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    records = [
        {"digest": "a" * 64, "at_ms": 9007199254740991},
        {"digest": "b" * 64, "at_ms": 9007199254740990},
    ]
    state = {
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
        "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
        "admissions": [],
        "consumed_keys": records,
    }
    await redis_client.set(state_key, json.dumps(state))
    assert await admissions[1].release("a" * 64) == "RELEASED"
    raw = await redis_client.get(state_key)
    assert raw is not None
    assert json.loads(raw) == {**state, "lease": None}
    result = await admissions[0].acquire(_USER, _REPOSITORY, b"large-timestamp")
    assert result.status == "ACQUIRED"
    raw = await redis_client.get(state_key)
    assert raw is not None
    assert json.loads(raw)["consumed_keys"][:2] == records


async def test_rotating_secret_requires_external_migration(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    result = await admissions[0].acquire(_USER, _REPOSITORY, b"rotation")
    assert result.lease is not None
    assert _REDIS_URL is not None
    rotated = RedisSimulationAdmission(
        _REDIS_URL,
        state_key=state_key,
        hmac_secret=b"different-test-only-secret-32bytes",
    )
    try:
        assert rotated.hmac_config_id != admissions[0].hmac_config_id
        before = await redis_client.get(state_key)
        assert (
            await rotated.acquire(_USER, _REPOSITORY, b"rotation")
        ).status == "INVALID_STATE"
        assert await rotated.release(result.lease.owner_token) == "INVALID_STATE"
        assert await redis_client.get(state_key) == before
        assert await admissions[0].release(result.lease.owner_token) == "RELEASED"
        assert (
            await admissions[1].acquire(_USER, _REPOSITORY, b"rotation")
        ).status == "DUPLICATE"
    finally:
        await rotated.aclose()


@pytest.mark.parametrize("missing", ["hmac_config_id", "consumed_keys"])
@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_v3_requires_configuration_and_consumed_history(
    missing: str,
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    state = {
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
        "consumed_keys": [],
        "admissions": [],
        "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
    }
    del state[missing]
    before = json.dumps(state)
    await redis_client.set(state_key, before)
    if operation == "acquire":
        assert (
            await admissions[0].acquire(_USER, _REPOSITORY, b"missing")
        ).status == "INVALID_STATE"
    else:
        assert await admissions[0].release("a" * 64) == "INVALID_STATE"
    assert await redis_client.get(state_key) == before


@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_v2_is_incompatible_and_never_migrated(
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    before = (
        '{"schema_version":2,"lease":{"owner_token":"'
        + "a" * 64
        + '","expires_at_ms":1},"admissions":[]}'
    )
    await redis_client.set(state_key, before)
    if operation == "acquire":
        result = await admissions[0].acquire(_USER, _REPOSITORY, b"legacy-state")
        assert result.status == "INVALID_STATE"
        assert result.lease is None
    else:
        assert await admissions[1].release("a" * 64) == "INVALID_STATE"
    assert await redis_client.get(state_key) == before


async def test_lease_timeout_does_not_make_consumed_key_available(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    result = await admissions[0].acquire(_USER, _REPOSITORY, b"lease-timeout")
    assert result.lease is not None
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    seconds, micros = await redis_client.time()
    state["lease"]["expires_at_ms"] = seconds * 1000 + micros // 1000 - 1
    before = json.dumps(state)
    await redis_client.set(state_key, before)
    duplicate = await admissions[1].acquire(_USER, _REPOSITORY, b"lease-timeout")
    assert duplicate.status == "DUPLICATE"
    assert duplicate.lease is None
    assert await redis_client.get(state_key) == before
    other = await admissions[1].acquire(_USER, _REPOSITORY, b"after-timeout")
    assert other.status == "ACQUIRED"
    assert await admissions[0].release(result.lease.owner_token) == "NOT_OWNER"


async def test_invalid_state_precedes_duplicate_even_with_active_lease(
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    result = await admissions[0].acquire(_USER, _REPOSITORY, b"invalid-first")
    assert result.lease is not None
    raw = await redis_client.get(state_key)
    assert raw is not None
    state = json.loads(raw)
    state["consumed_keys"].append({"digest": "z" * 64, "at_ms": 1})
    before = json.dumps(state)
    await redis_client.set(state_key, before)
    denied = await admissions[1].acquire(_USER, _REPOSITORY, b"invalid-first")
    assert denied.status == "INVALID_STATE"
    assert denied.lease is None
    assert await redis_client.get(state_key) == before


@pytest.mark.parametrize(
    "timestamp", ["9007199254740990.5", "1.0000000000000001", "1e0", "01", "0x1", "+1"]
)
@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_noncanonical_timestamp_tokens_fail_closed_before_numeric_rounding(
    timestamp: str,
    operation: str,
    redis_client: Redis,
    state_key: str,
    admissions: tuple[RedisSimulationAdmission, RedisSimulationAdmission],
) -> None:
    # Preserve raw JSON spelling: a Python float would round the invalid fixture.
    state = {
        "schema_version": 3,
        "hmac_config_id": _CONFIG_ID,
        "lease": {"owner_token": "a" * 64, "expires_at_ms": 1},
        "admissions": [],
        "consumed_keys": [{"digest": "a" * 64, "at_ms": "STAMP"}],
    }
    before = json.dumps(state).replace('"STAMP"', timestamp)
    await redis_client.set(state_key, before)
    if operation == "acquire":
        denied = await admissions[0].acquire(_USER, _REPOSITORY, b"fractional-state")
        assert denied.status == "INVALID_STATE"
        assert denied.lease is None
    else:
        assert await admissions[1].release("a" * 64) == "INVALID_STATE"
    assert await redis_client.get(state_key) == before
