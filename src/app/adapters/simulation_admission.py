"""Inactive Rule Playground lease interface; no HTTP or executor wiring.

Provision STATE_KEY separately as
``{"schema_version":2,"lease":null,"admissions":[]}``, without a physical TTL.
Version 1 is incompatible; migration/provisioning is external. Each admission
stores Redis TIME in milliseconds and trusted application user/repository UUIDs
as lowercase hex. No request content is stored. Acquisition grants a 90s lease.

State is bounded to 4096 bytes and 20 records (the global hourly ceiling).
Each record needs at most 133 bytes, plus at most 200 bytes for root/lease and
array separators: less than 3 KiB in the compact encoding. Records at or before
now - 3600000 expire. Future records count conservatively on clock rollback.
Release retains history. Denials never write or prune.
Deterministic check order: INVALID_STATE, BUSY,
RATE_USER (3), RATE_REPOSITORY (10), RATE_GLOBAL (20).

UNAVAILABLE never grants permission, but a lost reply can hide a committed write.
No automatic EVAL retry is made; reconciliation belongs to the later protocol.
Call aclose() when disposing the adapter and its dedicated pool.
"""

import secrets
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool, parse_url
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

STATE_KEY = "dmc268:rule-playground:admission"
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_EXPIRY_DIGITS = 16
type ReleaseResult = Literal["RELEASED", "NOT_OWNER", "INVALID_STATE", "UNAVAILABLE"]

_STATE = r"""
local function field_count(value)
    local count = 0
    for _ in pairs(value) do count = count + 1 end
    return count
end
-- Redis cjson represents both [] and {} as Lua tables. Tag JSON arrays before
-- decoding so the required admissions array cannot be confused with an object.
-- Strings (including escaped quotes/brackets) are copied verbatim.
local function decode_with_arrays(raw)
    local parts, quoted, escaped = {}, false, false
    for i = 1, #raw do
        local ch = string.sub(raw, i, i)
        if quoted then
            parts[#parts + 1] = ch
            if escaped then escaped = false
            elseif ch == '\\' then escaped = true
            elseif ch == '"' then quoted = false end
        elseif ch == '"' then
            quoted = true
            parts[#parts + 1] = ch
        elseif ch == '[' then parts[#parts + 1] = '{"array":['
        elseif ch == ']' then parts[#parts + 1] = ']}'
        else parts[#parts + 1] = ch end
    end
    local tagged = cjson.decode(table.concat(parts))
    local state = cjson.decode(raw)
    if type(state) ~= 'table' or type(state.admissions) ~= 'table'
        or field_count(state.admissions) ~= #state.admissions
        or type(tagged.admissions) ~= 'table'
        or type(tagged.admissions.array) ~= 'table' then return nil end
    return state
end
local function integer(value)
    return type(value) == 'number' and value == value and value >= 1
        and value <= 9007199254740991 and value == math.floor(value)
end
local function identity(value)
    return type(value) == 'string' and #value == 32
        and string.match(value, '^[0-9a-f]+$')
end
local function encode_state(state)
    local records = {}
    for i, admission in ipairs(state.admissions) do
        -- cjson's 14-digit encoder would round valid safe integers in history.
        -- Identities are validated lowercase hex, so they need no JSON escaping.
        records[i] = string.format(
            '{"at_ms":%.0f,"user_id":"%s","repository_id":"%s"}',
            admission.at_ms, admission.user_id, admission.repository_id
        )
    end
    return '{"schema_version":2,"lease":' .. cjson.encode(state.lease)
        .. ',"admissions":[' .. table.concat(records, ',') .. ']}'
end
local function load_state()
    if redis.call('TYPE', KEYS[1]).ok ~= 'string' then return nil end
    if redis.call('PTTL', KEYS[1]) ~= -1 then return nil end
    if redis.call('STRLEN', KEYS[1]) > 4096 then return nil end
    local ok, state = pcall(decode_with_arrays, redis.call('GET', KEYS[1]))
    if not ok or type(state) ~= 'table' then return nil end
    if state.schema_version ~= 2 or field_count(state) ~= 3 then return nil end
    local admissions = state.admissions
    if #admissions > 20 then return nil end
    for _, admission in ipairs(admissions) do
        if type(admission) ~= 'table' or field_count(admission) ~= 3
            or not integer(admission.at_ms) or not identity(admission.user_id)
            or not identity(admission.repository_id) then return nil end
    end
    state.admissions = admissions
    local lease = state.lease
    if lease == cjson.null then return state end
    if type(lease) ~= 'table' or field_count(lease) ~= 2 then return nil end
    if type(lease.owner_token) ~= 'string' or #lease.owner_token ~= 64
        or not string.match(lease.owner_token, '^[0-9a-f]+$') then return nil end
    local expires = lease.expires_at_ms
    if not integer(expires) then
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
local active, user_count, repository_count = {}, 0, 0
for _, admission in ipairs(state.admissions) do
    if admission.at_ms > now - 3600000 then
        active[#active + 1] = admission
        if admission.user_id == ARGV[2] then user_count = user_count + 1 end
        if admission.repository_id == ARGV[3] then
            repository_count = repository_count + 1
        end
    end
end
if user_count >= 3 then return {'RATE_USER'} end
if repository_count >= 10 then return {'RATE_REPOSITORY'} end
if #active >= 20 then return {'RATE_GLOBAL'} end
active[#active + 1] = {at_ms=now, user_id=ARGV[2], repository_id=ARGV[3]}
state.admissions = active
local expires = now + 90000
state.lease = {owner_token=ARGV[1], expires_at_ms=expires}
local encoded = encode_state(state)
if #encoded > 4096 then return {'INVALID_STATE'} end
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
local encoded = encode_state(state)
if #encoded > 4096 then return 'INVALID_STATE' end
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
    status: Literal[
        "ACQUIRED",
        "BUSY",
        "RATE_USER",
        "RATE_REPOSITORY",
        "RATE_GLOBAL",
        "INVALID_STATE",
        "UNAVAILABLE",
    ]
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

    async def acquire(self, user_id: UUID, repository_id: UUID) -> AdmissionResult:
        owner = secrets.token_hex(32)
        try:
            response = await self._client.eval(
                _ACQUIRE, 1, self._state_key, owner, user_id.hex, repository_id.hex
            )
        except RedisError:
            return AdmissionResult("UNAVAILABLE")
        match response:
            case ["ACQUIRED", str() as expires]:
                if (
                    not expires.isascii()
                    or not expires.isdecimal()
                    or len(expires) > _MAX_EXPIRY_DIGITS
                    or not 0 < int(expires) <= _MAX_SAFE_INTEGER
                ):
                    return AdmissionResult("UNAVAILABLE")
                return AdmissionResult("ACQUIRED", Lease(owner, int(expires)))
            case [
                "BUSY"
                | "RATE_USER"
                | "RATE_REPOSITORY"
                | "RATE_GLOBAL"
                | "INVALID_STATE" as status
            ]:
                return AdmissionResult(status)
            case _:
                return AdmissionResult("UNAVAILABLE")

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
