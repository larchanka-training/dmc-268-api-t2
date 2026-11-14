"""Pure HMAC input validation; diagnostics never include input values."""

import base64
import hmac
from uuid import UUID


def decode_simulation_hmac(encoded: str | None) -> bytes:
    try:
        if not encoded:
            raise ValueError
        secret = base64.b64decode(encoded, validate=True)
        if not 32 <= len(secret) <= 1024:  # noqa: PLR2004
            raise ValueError
        if base64.b64encode(secret).decode() != encoded:
            raise ValueError
        return secret
    except ValueError:
        raise ValueError("invalid_simulation_secret") from None


def simulation_operation_digest(
    secret: bytes, user_id: UUID, repository_id: UUID, idempotency_key: bytes
) -> str:
    """Stable Redis v3 dispatch identity, also used by the permanent PG ledger.

    No new domain: exact bytes, 8-byte big-endian length framing, UUID.bytes.
    Operation covers both Code and Diff, so switching input kind cannot replay.
    """
    fields = (
        b"dmc268:rule-simulation:dispatch:v1",
        user_id.bytes,
        repository_id.bytes,
        idempotency_key,
    )
    message = b"".join(len(field).to_bytes(8, "big") + field for field in fields)
    return hmac.digest(secret, message, "sha256").hex()


def simulation_hmac_config_id(secret: bytes) -> str:
    """Existing Redis v3 configuration identity (not a snapshot freshness proof)."""
    return hmac.digest(secret, b"dmc268:rule-simulation:hmac-config:v1", "sha256").hex()
