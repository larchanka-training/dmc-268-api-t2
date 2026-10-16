"""OAuth query credentials must not reach application log outputs."""

import logging
from pathlib import Path

import pytest

from app.core.config import Logging
from app.core.logging_config import configure_logging


@pytest.mark.parametrize("format_name", ["console", "json"])
def test_oauth_callback_queries_are_redacted_from_access_and_http_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], format_name: str
) -> None:
    log_path = tmp_path / "auth.log"
    configure_logging(
        Logging.model_validate({"format": format_name, "file_path": log_path})
    )
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:123",
        "GET",
        "/api/v1/auth/github/callback?code=private-oauth-code&state=private-flow-state",
        "1.1",
        303,
    )
    logging.getLogger("httpx").info(
        "HTTP Request: GET https://test/api/v1/auth/github/callback"
        '?code=private-oauth-code&state=private-flow-state "HTTP/1.1 303 See Other"'
    )
    for output in [capsys.readouterr().out, log_path.read_text()]:
        assert "private-oauth-code" not in output
        assert "private-flow-state" not in output
        assert "/api/v1/auth/github/callback" in output
        assert "303" in output
