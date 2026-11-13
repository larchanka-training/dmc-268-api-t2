"""Playground content limits at the authenticated HTTP boundary."""

import json
from http import HTTPStatus

import httpx
import pytest
from test_postgres_integration import PostgresHarness
from test_rule_simulations_access import SimulationScope, assert_simulation_response

pytest_plugins = ("test_rule_simulations_access",)


def _sized_git_diff(size: int, char: str = "x") -> str:
    # One changed line in Python avoids generated/minified-file policy skips.
    prefix = (
        "diff --git a/private.py b/private.py\n"
        "--- a/private.py\n+++ b/private.py\n@@ -0,0 +1 @@\n+"
    )
    available = size - len(prefix.encode()) - 1
    count, remainder = divmod(available, len(char.encode("utf-8")))
    diff = prefix + char * count + "x" * remainder + "\n"
    assert len(diff.encode("utf-8")) == size
    return diff


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/private/file.py", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("", HTTPStatus.UNPROCESSABLE_ENTITY),
        (".", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("..", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("private/../file.py", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("private/./file.py", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("private//file.py", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("private/file.py/", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("private\\file.py", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("private/\x00file.py", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("src/пример/file.py", HTTPStatus.SERVICE_UNAVAILABLE),
    ],
)
async def test_path_normalization(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and content vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    path: str,
    status: HTTPStatus,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "code", "path": path, "source": "private-source"},
        status,
    )


@pytest.mark.parametrize("drive", ["C:/private/file.py", "z:/private/file.py"])
async def test_path_rejects_drive_absolute_labels(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    drive: str,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "code", "path": drive, "source": "private-source"},
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )


@pytest.mark.parametrize("control", [*range(1, 32), 127])
async def test_path_rejects_ascii_controls_and_del(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    control: int,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "code", "path": f"private/{chr(control)}file.py", "source": "x"},
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )


@pytest.mark.parametrize(
    ("path", "status"),
    [
        pytest.param("a" * 513, HTTPStatus.UNPROCESSABLE_ENTITY, id="ascii-513"),
        pytest.param("я" * 256 + "a", HTTPStatus.UNPROCESSABLE_ENTITY, id="utf8-513"),
        pytest.param(
            "😀" * 128 + "a", HTTPStatus.UNPROCESSABLE_ENTITY, id="utf8-4byte-513"
        ),
        pytest.param("a" * 512, HTTPStatus.SERVICE_UNAVAILABLE, id="ascii-512"),
        pytest.param("я" * 256, HTTPStatus.SERVICE_UNAVAILABLE, id="utf8-512"),
        pytest.param("😀" * 128, HTTPStatus.SERVICE_UNAVAILABLE, id="utf8-4byte-512"),
    ],
)
async def test_path_utf8_byte_boundary(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and content vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    path: str,
    status: HTTPStatus,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "code", "path": path, "source": "x"},
        status,
    )


@pytest.mark.parametrize("source", ["", " \t\r\n", "\u2003\u00a0\u2028"])
async def test_code_source_must_not_be_blank(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    source: str,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "code", "path": "private.py", "source": source},
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )


@pytest.mark.parametrize(
    ("source", "status"),
    [
        pytest.param("x" * 65537, HTTPStatus.UNPROCESSABLE_ENTITY, id="ascii-65537"),
        pytest.param(
            "я" * 32768 + "x", HTTPStatus.UNPROCESSABLE_ENTITY, id="utf8-65537"
        ),
        pytest.param(
            "😀" * 16384 + "x", HTTPStatus.UNPROCESSABLE_ENTITY, id="utf8-4byte-65537"
        ),
        pytest.param("x" * 65536, HTTPStatus.SERVICE_UNAVAILABLE, id="ascii-65536"),
        pytest.param("я" * 32768, HTTPStatus.SERVICE_UNAVAILABLE, id="utf8-65536"),
        pytest.param(
            "😀" * 16384, HTTPStatus.SERVICE_UNAVAILABLE, id="utf8-4byte-65536"
        ),
    ],
)
async def test_code_source_utf8_byte_boundary(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and content vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    source: str,
    status: HTTPStatus,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "code", "path": "private.py", "source": source},
        status,
    )


@pytest.mark.parametrize(
    ("source", "status"),
    [
        pytest.param(
            "\n".join(["я"] * 501), HTTPStatus.UNPROCESSABLE_ENTITY, id="lf-501"
        ),
        pytest.param("я\n" * 501, HTTPStatus.UNPROCESSABLE_ENTITY, id="lf-501-final"),
        pytest.param(
            "\r\n".join(["я"] * 501), HTTPStatus.UNPROCESSABLE_ENTITY, id="crlf-501"
        ),
        pytest.param(
            "я\r\n" * 501, HTTPStatus.UNPROCESSABLE_ENTITY, id="crlf-501-final"
        ),
        pytest.param(
            "я\n" * 500 + "\n",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            id="lf-extra-blank-line",
        ),
        pytest.param(
            "я\r\n" * 500 + "\r\n",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            id="crlf-extra-blank-line",
        ),
        pytest.param(
            "\n".join(["я"] * 500), HTTPStatus.SERVICE_UNAVAILABLE, id="lf-500"
        ),
        pytest.param("я\n" * 500, HTTPStatus.SERVICE_UNAVAILABLE, id="lf-500-final"),
        pytest.param(
            "\r\n".join(["я"] * 500), HTTPStatus.SERVICE_UNAVAILABLE, id="crlf-500"
        ),
        pytest.param(
            "я\r\n" * 500, HTTPStatus.SERVICE_UNAVAILABLE, id="crlf-500-final"
        ),
        pytest.param("я\r" * 500, HTTPStatus.SERVICE_UNAVAILABLE, id="cr-500-final"),
        pytest.param("я\r" * 501, HTTPStatus.UNPROCESSABLE_ENTITY, id="cr-501-final"),
        pytest.param(
            "я\u2028" * 500, HTTPStatus.SERVICE_UNAVAILABLE, id="unicode-500-final"
        ),
        pytest.param(
            "я\u2028" * 501, HTTPStatus.UNPROCESSABLE_ENTITY, id="unicode-501-final"
        ),
    ],
)
async def test_code_source_logical_line_boundary(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and content vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    source: str,
    status: HTTPStatus,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "code", "path": "private.py", "source": source},
        status,
    )


@pytest.mark.parametrize("diff", ["", " \t\r\n", "\u2003\u00a0\u2028"])
async def test_diff_must_not_be_blank(
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    diff: str,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "diff", "unified_diff": diff},
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )


@pytest.mark.parametrize(
    ("diff", "status"),
    [
        pytest.param(
            _sized_git_diff(131073), HTTPStatus.UNPROCESSABLE_ENTITY, id="ascii-131073"
        ),
        pytest.param(
            _sized_git_diff(131073, "я"),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            id="utf8-131073",
        ),
        pytest.param(
            _sized_git_diff(131073, "😀"),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            id="utf8-4byte-131073",
        ),
        pytest.param(
            _sized_git_diff(131072), HTTPStatus.SERVICE_UNAVAILABLE, id="ascii-131072"
        ),
        pytest.param(
            _sized_git_diff(131072, "я"),
            HTTPStatus.SERVICE_UNAVAILABLE,
            id="utf8-131072",
        ),
        pytest.param(
            _sized_git_diff(131072, "😀"),
            HTTPStatus.SERVICE_UNAVAILABLE,
            id="utf8-4byte-131072",
        ),
    ],
)
async def test_diff_utf8_byte_boundary(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and content vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    diff: str,
    status: HTTPStatus,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        {"kind": "diff", "unified_diff": diff},
        status,
    )


@pytest.mark.parametrize(
    "input_value",
    [
        pytest.param(
            {
                "kind": "code",
                "path": "../stage2c-private-file.py",
                "source": "stage2c-private-source",
            },
            id="path",
        ),
        pytest.param(
            {
                "kind": "code",
                "path": "stage2c-private-file.py",
                "source": "stage2c-private-source" + "x" * 65536,
            },
            id="source",
        ),
        pytest.param(
            {"kind": "diff", "unified_diff": "stage2c-private-diff" + "x" * 131072},
            id="diff",
        ),
    ],
)
async def test_content_errors_preserve_safe_envelope_and_do_not_log_input(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and content vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
    input_value: dict[str, str],
) -> None:
    # Error projection is an existing contract, exercised with content errors.
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        input_value,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )
    assert "stage2c-private" not in caplog.text


@pytest.mark.parametrize(
    ("input_value", "status"),
    [
        pytest.param(
            {"kind": "code", "path": "я" * 256, "source": "x"},
            HTTPStatus.SERVICE_UNAVAILABLE,
            id="path-512",
        ),
        pytest.param(
            {"kind": "code", "path": "я" * 256 + "x", "source": "x"},
            HTTPStatus.UNPROCESSABLE_ENTITY,
            id="path-513",
        ),
        pytest.param(
            {"kind": "code", "path": "x.py", "source": "я" * 32768},
            HTTPStatus.SERVICE_UNAVAILABLE,
            id="source-65536",
        ),
        pytest.param(
            {"kind": "code", "path": "x.py", "source": "я" * 32768 + "x"},
            HTTPStatus.UNPROCESSABLE_ENTITY,
            id="source-65537",
        ),
        pytest.param(
            {"kind": "diff", "unified_diff": _sized_git_diff(131072)[:-3] + "я\n"},
            HTTPStatus.SERVICE_UNAVAILABLE,
            id="diff-131072",
        ),
        pytest.param(
            {"kind": "diff", "unified_diff": _sized_git_diff(131072)[:-3] + "яx\n"},
            HTTPStatus.UNPROCESSABLE_ENTITY,
            id="diff-131073",
        ),
    ],
)
async def test_byte_limits_apply_after_json_escape_decoding(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and content vectors
    browser: httpx.AsyncClient,
    simulation_scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    input_value: dict[str, str],
    status: HTTPStatus,
) -> None:
    await _assert_content(
        browser,
        simulation_scope,
        postgres,
        github_requests,
        input_value,
        status,
        ensure_ascii=True,
    )


async def _assert_content(  # noqa: PLR0913, PLR0917 -- explicit HTTP fixtures and expectations
    browser: httpx.AsyncClient,
    scope: SimulationScope,
    postgres: PostgresHarness,
    github_requests: list[httpx.Request],
    input_value: dict[str, str],
    status: HTTPStatus,
    *,
    ensure_ascii: bool = False,
) -> None:
    invalid = status == HTTPStatus.UNPROCESSABLE_ENTITY
    await assert_simulation_response(
        browser,
        scope,
        postgres,
        github_requests,
        scope.repository,
        status,
        "VALIDATION_FAILED" if invalid else "SERVICE_UNAVAILABLE",
        "Request is invalid." if invalid else "Service is unavailable.",
        content=json.dumps(
            {
                "input": input_value,
                "rules": {
                    "format": "rules-md/1",
                    "markdown": (
                        "# Security\n## SEC-1: stage2c-private-markdown <secret>\n"
                        "Check the input."
                    ),
                },
            },
            ensure_ascii=ensure_ascii,
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
