"""Pure HMAC input validation; diagnostics never include input values."""

import base64


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
