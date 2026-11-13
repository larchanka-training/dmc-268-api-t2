"""Inactive Rule Simulation admission; no HTTP or executor wiring.

Externally provision STATE_KEY without a physical TTL as:
{"schema_version":3,"lease":null,"admissions":[],"consumed_keys":[],
 "hmac_config_id":"<adapter.hmac_config_id>"}.
Versions 1/2 are incompatible: no automatic migration, repair or provisioning.

Pass a stable, securely generated 32..1024-byte hmac_secret explicitly. Missing
or malformed configuration fails closed before EVAL. hmac_config_id is a separate
HMAC-SHA256 of a fixed configuration domain, never the secret. Rotation requires
external migration; mismatching configuration fails closed on acquire/release.

acquire takes trusted UUIDs and the exact Idempotency-Key bytes. Its HMAC-SHA256
message consists of 8-byte unsigned big-endian length + value for each of:
fixed Rule Simulation operation, user UUID bytes, repository UUID bytes, raw key.
The key and request body never enter Redis; body is not part of this interface.
No other operation identity is supported. Redis stores only the digest and Redis
TIME admission timestamp for each consumed key. Release, failed execution,
timeout and cancellation cannot undo consumption. Deduplication lasts 24 hours.

Concurrency is 1, lease is 90s, rolling hourly ceilings are user 3, repository 10,
global 20. At most 24 * 20 = 480 admissions fit in a rolling 24-hour interval.
Compact encoding uses at most 102 bytes per consumed record, 122 per rate record,
plus separators/root/lease/config: 52,161 bytes with 480 + 20 records and maximal
safe-integer timestamps. Numeric tokens must be canonical positive decimal
integers, checked before cjson decoding to prevent fractional rounding.
Bound input/output to 65,536 bytes and 480/20 records.
On clock rollback, future records count conservatively; capacity fails closed
without evicting any unexpired key. Timestamps serialize as exact integers.

Records at or before now - retention expire (24h consumed, 1h rate). Only a
successful admission prunes; denials never write, release retains both histories.
Check order: INVALID_STATE, DUPLICATE, BUSY, RATE_USER, RATE_REPOSITORY, RATE_GLOBAL.
A successful mutation uses one final SET on one root key, with no physical TTL.

UNAVAILABLE never grants permission, but a lost reply can hide a committed write.
Zero EVAL retries; response recovery/reconciliation belongs to C2.4. This is
suppression, not an exactly-once execution guarantee. Call aclose() on disposal.
"""

import hmac
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
_OPERATION = b"dmc268:rule-simulation:dispatch:v1"
_CONFIG_DOMAIN = b"dmc268:rule-simulation:hmac-config:v1"
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_EXPIRY_DIGITS = 16
_MIN_SECRET_BYTES = 32
_MAX_SECRET_BYTES = 1024
type ReleaseResult = Literal["RELEASED", "NOT_OWNER", "INVALID_STATE", "UNAVAILABLE"]

_STATE = r"""
local function field_count(value)
    local count = 0
    for _ in pairs(value) do count = count + 1 end
    return count
end
-- Redis cjson represents both [] and {} as Lua tables. Tag JSON arrays before
-- decoding so required history arrays cannot be confused with objects.
-- Strings (including escaped quotes/brackets) are copied verbatim.
local function decode_with_arrays(raw)
    local parts, quoted, escaped, number = {}, false, false, false
    for i = 1, #raw do
        local ch = string.sub(raw, i, i)
        -- Validate numeric lexemes before cjson can round fractional/unsafe
        -- spellings into valid integers. All v3 numbers are positive decimals.
        if not quoted then
            if ch >= '0' and ch <= '9' then
                if not number and ch == '0' then return nil end
                number = true
            else
                if number and not string.match(ch, '^[%s,%]%}]$') then
                    return nil
                end
                number = false
                if ch == '-' or ch == '+' or ch == '.' then return nil end
            end
        end
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
    if type(state.consumed_keys) ~= 'table'
        or field_count(state.consumed_keys) ~= #state.consumed_keys
        or type(tagged.consumed_keys) ~= 'table'
        or type(tagged.consumed_keys.array) ~= 'table' then return nil end
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
    local consumed = {}
    for i, record in ipairs(state.consumed_keys) do
        consumed[i] = string.format('{"digest":"%s","at_ms":%.0f}',
            record.digest, record.at_ms)
    end
    return '{"schema_version":3,"lease":' .. cjson.encode(state.lease)
        .. ',"admissions":[' .. table.concat(records, ',') .. ']'
        .. ',"hmac_config_id":' .. cjson.encode(state.hmac_config_id)
        .. ',"consumed_keys":[' .. table.concat(consumed, ',') .. ']}'
end
local function load_state(expected_config)
    if redis.call('TYPE', KEYS[1]).ok ~= 'string' then return nil end
    if redis.call('PTTL', KEYS[1]) ~= -1 then return nil end
    if redis.call('STRLEN', KEYS[1]) > 65536 then return nil end
    local ok, state = pcall(decode_with_arrays, redis.call('GET', KEYS[1]))
    if not ok or type(state) ~= 'table' then return nil end
    if state.schema_version ~= 3 or field_count(state) ~= 5 then return nil end
    if type(state.hmac_config_id) ~= 'string' or #state.hmac_config_id ~= 64
        or not string.match(state.hmac_config_id, '^[0-9a-f]+$')
        or state.hmac_config_id ~= expected_config then return nil end
    if #state.consumed_keys > 480 then return nil end
    local seen = {}
    for _, record in ipairs(state.consumed_keys) do
        if type(record) ~= 'table' or field_count(record) ~= 2
            or type(record.digest) ~= 'string' or #record.digest ~= 64
            or not string.match(record.digest, '^[0-9a-f]+$')
            or not integer(record.at_ms) or seen[record.digest] then return nil end
        seen[record.digest] = true
    end
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
"""

_ACQUIRE = (
    _STATE
    + """
local state = load_state(ARGV[5])
if not state then return {'INVALID_STATE'} end
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
local consumed = {}
for _, record in ipairs(state.consumed_keys) do
    if record.at_ms > now - 86400000 then
        if record.digest == ARGV[4] then return {'DUPLICATE'} end
        consumed[#consumed + 1] = record
    end
end
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
if #consumed >= 480 then return {'INVALID_STATE'} end
consumed[#consumed + 1] = {digest=ARGV[4], at_ms=now}
state.consumed_keys = consumed
local expires = now + 90000
state.lease = {owner_token=ARGV[1], expires_at_ms=expires}
local encoded = encode_state(state)
if #encoded > 65536 then return {'INVALID_STATE'} end
redis.call('SET', KEYS[1], encoded)
return {'ACQUIRED', tostring(expires)}
"""
)

_RELEASE = (
    _STATE
    + """
local state = load_state(ARGV[2])
if not state then return 'INVALID_STATE' end
if state.lease == cjson.null or state.lease.owner_token ~= ARGV[1] then
    return 'NOT_OWNER'
end
state.lease = cjson.null
local encoded = encode_state(state)
if #encoded > 65536 then return 'INVALID_STATE' end
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
        "DUPLICATE",
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
    def __init__(
        self, url: str, *, hmac_secret: bytes | None = None, state_key: str = STATE_KEY
    ) -> None:
        self._client = create_admission_client(url)
        self._state_key = state_key
        self._hmac_secret = (
            hmac_secret
            if isinstance(hmac_secret, bytes)
            and _MIN_SECRET_BYTES <= len(hmac_secret) <= _MAX_SECRET_BYTES
            else None
        )
        self._hmac_config_id = (
            hmac.digest(self._hmac_secret, _CONFIG_DOMAIN, "sha256").hex()
            if self._hmac_secret is not None
            else None
        )

    @property
    def hmac_config_id(self) -> str | None:
        """Safe, derived configuration identity for external v3 provisioning."""
        return self._hmac_config_id

    async def aclose(self) -> None:
        await self._client.aclose()

    async def acquire(
        self, user_id: UUID, repository_id: UUID, idempotency_key: bytes
    ) -> AdmissionResult:
        if self._hmac_secret is None or self._hmac_config_id is None:
            return AdmissionResult("UNAVAILABLE")
        # Fixed operation + UUID bytes + exact key bytes, each length-prefixed.
        fields = (_OPERATION, user_id.bytes, repository_id.bytes, idempotency_key)
        message = b"".join(len(field).to_bytes(8, "big") + field for field in fields)
        digest = hmac.digest(self._hmac_secret, message, "sha256").hex()
        owner = secrets.token_hex(32)
        try:
            response = await self._client.eval(
                _ACQUIRE,
                1,
                self._state_key,
                owner,
                user_id.hex,
                repository_id.hex,
                digest,
                self._hmac_config_id,
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
                "DUPLICATE"
                | "BUSY"
                | "RATE_USER"
                | "RATE_REPOSITORY"
                | "RATE_GLOBAL"
                | "INVALID_STATE" as status
            ]:
                return AdmissionResult(status)
            case _:
                return AdmissionResult("UNAVAILABLE")

    async def release(self, owner_token: str) -> ReleaseResult:
        if self._hmac_secret is None or self._hmac_config_id is None:
            return "UNAVAILABLE"
        try:
            response: object = await self._client.eval(
                _RELEASE, 1, self._state_key, owner_token, self._hmac_config_id
            )
        except RedisError:
            return "UNAVAILABLE"
        match response:
            case "RELEASED" | "NOT_OWNER" | "INVALID_STATE" as status:
                return status
            case _:
                return "UNAVAILABLE"
