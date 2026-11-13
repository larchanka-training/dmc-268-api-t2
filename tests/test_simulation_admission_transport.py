"""Inspect effective redis-py connections, including conflicting URL options."""

import pytest
from redis.backoff import NoBackoff

from app.adapters.simulation_admission import create_admission_client

_SOCKET_TIMEOUT_SECONDS = 0.5
_RESP2 = 2


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
