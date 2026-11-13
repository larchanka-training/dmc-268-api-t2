"""Inactive Rule Playground lease interface; no HTTP or executor wiring.

Provision STATE_KEY separately as ``{"schema_version":1,"lease":null}``, without
a physical TTL. Only this metadata and a 256-bit owner / Redis-time expiry pair
are accepted (at most 1024 bytes). Acquisition grants one slot for 90 seconds.

UNAVAILABLE never grants permission, but a lost reply can hide a committed write.
No automatic EVAL retry is made; reconciliation belongs to the later protocol.
Call aclose() when disposing the adapter and its dedicated pool.
"""

import secrets
from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool, parse_url
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

STATE_KEY = "dmc268:rule-playground:admission"
type ReleaseResult = Literal["RELEASED", "NOT_OWNER", "INVALID_STATE", "UNAVAILABLE"]

_STATE = """
local function field_count(value)
    local count = 0
    for _ in pairs(value) do count = count + 1 end
    return count
end
local function load_state()
    if redis.call('TYPE', KEYS[1]).ok ~= 'string' then return nil end
    if redis.call('PTTL', KEYS[1]) ~= -1 then return nil end
    if redis.call('STRLEN', KEYS[1]) > 1024 then return nil end
    local ok, state = pcall(cjson.decode, redis.call('GET', KEYS[1]))
    if not ok or type(state) ~= 'table' then return nil end
    if state.schema_version ~= 1 or field_count(state) ~= 2 then return nil end
    local lease = state.lease
    if lease == cjson.null then return state end
    if type(lease) ~= 'table' or field_count(lease) ~= 2 then return nil end
    if type(lease.owner_token) ~= 'string' or #lease.owner_token ~= 64
        or not string.match(lease.owner_token, '^[0-9a-f]+$') then return nil end
    local expires = lease.expires_at_ms
    if type(expires) ~= 'number' or expires ~= expires or expires < 1
        or expires > 9007199254740991 or expires ~= math.floor(expires) then
        return nil
    end
    return state
end
local state = load_state()
"""

_ACQUIRE = (
    _STATE
    + """
if not state then return {'INVALID_STATE'} end
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
if state.lease ~= cjson.null and state.lease.expires_at_ms > now then
    return {'BUSY'}
end
local expires = now + 90000
state.lease = {owner_token=ARGV[1], expires_at_ms=expires}
local encoded = cjson.encode(state)
redis.call('SET', KEYS[1], encoded)
return {'ACQUIRED', tostring(expires)}
"""
)

_RELEASE = (
    _STATE
    + """
if not state then return 'INVALID_STATE' end
if state.lease == cjson.null or state.lease.owner_token ~= ARGV[1] then
    return 'NOT_OWNER'
end
state.lease = cjson.null
local encoded = cjson.encode(state)
redis.call('SET', KEYS[1], encoded)
return 'RELEASED'
"""
)


@dataclass(frozen=True, slots=True)
class Lease:
    owner_token: str
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    status: Literal["ACQUIRED", "BUSY", "INVALID_STATE", "UNAVAILABLE"]
    lease: Lease | None = None


def create_admission_client(url: str) -> Redis:
    """Create a pool owned only by admission, never shared with the review queue."""
    # from_url lets query options override kwargs. Enforce policy after parsing.
    options: dict[str, Any] = dict(parse_url(url))
    options.update(
        retry=Retry(NoBackoff(), 0),
        retry_on_timeout=False,
        retry_on_error=[],
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        decode_responses=True,
        # RESP2 avoids RESP3 maintenance notifications relaxing socket timeouts.
        protocol=2,
    )
    return Redis.from_pool(ConnectionPool(**options))


class RedisSimulationAdmission:
    def __init__(self, url: str, *, state_key: str = STATE_KEY) -> None:
        self._client = create_admission_client(url)
        self._state_key = state_key

    async def aclose(self) -> None:
        await self._client.aclose()

    async def acquire(self) -> AdmissionResult:
        owner = secrets.token_hex(32)
        try:
            response = await self._client.eval(_ACQUIRE, 1, self._state_key, owner)
        except RedisError:
            return AdmissionResult("UNAVAILABLE")
        if response[0] == "ACQUIRED":
            return AdmissionResult("ACQUIRED", Lease(owner, int(response[1])))
        if response[0] == "INVALID_STATE":
            return AdmissionResult("INVALID_STATE")
        return AdmissionResult("BUSY")

    async def release(self, owner_token: str) -> ReleaseResult:
        try:
            response = await self._client.eval(
                _RELEASE, 1, self._state_key, owner_token
            )
        except RedisError:
            return "UNAVAILABLE"
        if response == "INVALID_STATE":
            return "INVALID_STATE"
        return "RELEASED" if response == "RELEASED" else "NOT_OWNER"
