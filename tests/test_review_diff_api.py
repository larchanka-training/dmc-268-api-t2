"""Stored diff reads through HTTP with real PostgreSQL and no provider traffic."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from test_postgres_integration import PostgresHarness
from test_review_results_api import NOW, assert_error, assert_schema
from test_review_stream_auth import StreamAuthScope

from app.db import models
from app.db.session import get_session

pytest_plugins = ("test_review_results_api", "test_review_stream_auth")


def saved_file(
    status: str = "modified", path: str = "src/a.py", *, offset: int = 1
) -> dict[str, Any]:
    return {
        "diff": {
            "file_id": "file_1",
            "status": status,
            "old_path": None
            if status == "added"
            else ("src/old.py" if status == "renamed" else path),
            "new_path": None if status == "deleted" else path,
            "old_blob_sha": "internal-old-sha",
            "new_blob_sha": "internal-new-sha",
            "language": "python",
            "hunks": [
                {
                    "hunk_id": "hunk_1",
                    "old_start": offset,
                    "old_count": 1,
                    "new_start": offset,
                    "new_count": 1,
                    "lines": [
                        {
                            "kind": "deleted",
                            "text": "old()",
                            "old_line": offset,
                            "new_line": None,
                        },
                        {
                            "kind": "added",
                            "text": "new()",
                            "old_line": None,
                            "new_line": offset,
                        },
                    ],
                }
            ],
        },
        "source_windows": [{"source": "private-source"}],
        "whole_file": "private-whole-file",
        "provider_metadata": "private-provider",
    }


async def save_files(
    postgres: PostgresHarness,
    review: uuid.UUID,
    files: list[dict[str, Any]],
    *,
    version: int = 1,
) -> uuid.UUID:
    context = postgres.track("context_payload", uuid.uuid4())
    async with postgres.engine.begin() as connection:
        await connection.execute(
            insert(models.ContextPayload).values(
                id=context,
                review_job_id=review,
                schema_version=version,
                created_at=NOW,
                payload_body={"files": files, "prompt": "private-prompt"},
            )
        )
    return context


async def test_diff_projects_all_file_statuses_without_internal_data_or_github(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbid(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("saved diff must never make HTTP provider requests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbid)
    files = [
        saved_file("modified"),
        saved_file("added", "new.py"),
        saved_file("deleted", "removed.py"),
        saved_file("renamed", "renamed.py"),
    ]
    files[0]["diff"]["hunks"][0]["lines"] = [
        {"kind": "context", "text": "same()", "old_line": 1, "new_line": 1}
    ]
    files[1]["diff"]["hunks"][0].update(old_start=0, old_count=0)
    files[1]["diff"]["hunks"][0]["lines"] = [
        {"kind": "added", "text": "new()", "old_line": None, "new_line": 1}
    ]
    files[2]["diff"]["hunks"][0].update(new_start=0, new_count=0)
    files[2]["diff"]["hunks"][0]["lines"] = [
        {"kind": "deleted", "text": "old()", "old_line": 1, "new_line": None}
    ]
    files[3]["diff"]["hunks"] = []
    await save_files(postgres, review, files)
    response = await client.get(f"/api/v1/reviews/{review}/diff")
    assert response.status_code == HTTPStatus.OK
    assert_schema(response, "Diff")
    assert response.json() == {
        "files": [
            {
                "path": "src/a.py",
                "previous_path": None,
                "status": "modified",
                "hunks": [
                    {
                        "old_start": 1,
                        "old_lines": 1,
                        "new_start": 1,
                        "new_lines": 1,
                        "lines": [
                            {
                                "kind": "context",
                                "content": "same()",
                                "old_line": 1,
                                "new_line": 1,
                            }
                        ],
                    }
                ],
            },
            {
                "path": "new.py",
                "previous_path": None,
                "status": "added",
                "hunks": [
                    {
                        "old_start": 0,
                        "old_lines": 0,
                        "new_start": 1,
                        "new_lines": 1,
                        "lines": [
                            {
                                "kind": "added",
                                "content": "new()",
                                "old_line": None,
                                "new_line": 1,
                            }
                        ],
                    }
                ],
            },
            {
                "path": "removed.py",
                "previous_path": None,
                "status": "deleted",
                "hunks": [
                    {
                        "old_start": 1,
                        "old_lines": 1,
                        "new_start": 0,
                        "new_lines": 0,
                        "lines": [
                            {
                                "kind": "deleted",
                                "content": "old()",
                                "old_line": 1,
                                "new_line": None,
                            }
                        ],
                    }
                ],
            },
            {
                "path": "renamed.py",
                "previous_path": "src/old.py",
                "status": "renamed",
                "hunks": [],
            },
        ]
    }


async def test_diff_merges_chunks_and_deduplicates_files_and_hunks(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
) -> None:
    late = saved_file(offset=8)
    early = saved_file(offset=1)
    duplicate = copy.deepcopy(early)
    duplicate["diff"]["file_id"] = "another-chunk-file-id"
    duplicate["diff"]["hunks"][0]["hunk_id"] = "another-chunk-hunk-id"
    await save_files(postgres, review, [late, early])
    await save_files(postgres, review, [duplicate])
    response = await client.get(f"/api/v1/reviews/{review}/diff")
    assert response.status_code == HTTPStatus.OK
    assert_schema(response, "Diff")
    assert len(response.json()["files"]) == 1
    hunks = response.json()["files"][0]["hunks"]
    assert [hunk["old_start"] for hunk in hunks] == [1, 8]
    assert (
        response.json() == (await client.get(f"/api/v1/reviews/{review}/diff")).json()
    )


@pytest.mark.parametrize(
    "defect",
    [
        "version",
        "files_shape",
        "missing_diff",
        "path",
        "status",
        "line_kind",
        "text_type",
        "bool_coordinate",
        "counts",
        "sequence",
        "side",
        "conflicting_file",
        "overlap",
        "negative",
        "start",
        "missing_line",
    ],
)
async def test_malformed_saved_diff_is_safe_and_never_partially_returned(  # noqa: PLR0912, PLR0915 -- historical corruption matrix
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
    defect: str,
) -> None:
    file = saved_file()
    diff = file["diff"]
    hunk = diff["hunks"][0]
    line = hunk["lines"][0]
    files = [file]
    version = 1
    match defect:
        case "version":
            version = 2
        case "files_shape":
            files = "private-malformed-body"  # type: ignore[assignment]
        case "missing_diff":
            del file["diff"]
        case "path":
            diff["new_path"] = "../private-path"
        case "status":
            diff["status"] = "private-invalid-status"
        case "line_kind":
            line["kind"] = "private-invalid-kind"
        case "text_type":
            line["text"] = {"private": "metadata"}
        case "bool_coordinate":
            line["old_line"] = True
        case "counts":
            hunk["old_count"] = 9
        case "sequence":
            line["old_line"] = 9
        case "side":
            line["new_line"] = 1
        case "negative":
            hunk["old_start"] = -1
        case "start":
            hunk["old_start"] = 0
        case "missing_line":
            del line["text"]
        case "conflicting_file":
            other = copy.deepcopy(file)
            other["diff"]["status"] = "renamed"
            other["diff"]["old_path"] = "previous.py"
            files.append(other)
        case "overlap":
            other = copy.deepcopy(file)
            other["diff"]["hunks"][0]["lines"][0]["text"] = "conflicting()"
            files.append(other)
    await save_files(postgres, review, files, version=version)
    response = await client.get(f"/api/v1/reviews/{review}/diff")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert_schema(response, "Error")
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["message"] == "Stored diff is unavailable."
    assert "private" not in response.text


@pytest.mark.parametrize("partial", [False, True])
async def test_retention_of_result_context_returns_410_even_for_partial_loss(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
    partial: bool,
) -> None:
    context = await save_files(postgres, review, [saved_file()])
    if partial:
        await save_files(postgres, review, [saved_file(path="kept.py")])
    async with postgres.engine.begin() as connection:
        await connection.execute(
            insert(models.ChunkResult).values(
                id=postgres.track("chunk_result", uuid.uuid4()),
                review_job_id=review,
                context_payload_id=context,
                schema_version=1,
                summary="safe summary",
                limitations=[],
                usage=None,
                latency_ms=1,
                created_at=NOW,
            )
        )
        await connection.execute(
            delete(models.ContextPayload).where(models.ContextPayload.id == context)
        )
    response = await client.get(f"/api/v1/reviews/{review}/diff")
    assert response.status_code == HTTPStatus.GONE
    assert_schema(response, "Error")
    assert response.json()["code"] == "DIFF_EXPIRED"
    assert response.json()["message"] == "Stored diff has expired."


@pytest.mark.parametrize(
    "state", ["QUEUED", "COMPLETED", "SKIPPED", "deleted_without_result"]
)
async def test_diff_without_retention_evidence_is_not_expired_and_empty_is_explicit(
    client: httpx.AsyncClient,
    review: uuid.UUID,
    principal: uuid.UUID,
    postgres: PostgresHarness,
    state: str,
) -> None:
    async with postgres.engine.begin() as connection:
        if state != "deleted_without_result":
            await connection.execute(
                update(models.ReviewJob)
                .where(models.ReviewJob.id == review)
                .values(status=state, finished_at=None if state == "QUEUED" else NOW)
            )
    if state == "deleted_without_result":
        context = await save_files(postgres, review, [saved_file()])
        async with postgres.engine.begin() as connection:
            await connection.execute(
                delete(models.ContextPayload).where(models.ContextPayload.id == context)
            )
    response = await client.get(f"/api/v1/reviews/{review}/diff")
    assert_error(response, 404, "RESOURCE_NOT_FOUND")
    await save_files(postgres, review, [])
    empty = await client.get(f"/api/v1/reviews/{review}/diff")
    assert empty.status_code == HTTPStatus.OK
    assert empty.json() == {"files": []}
    assert_schema(empty, "Diff")


@pytest_asyncio.fixture
async def diff_session_auth(
    stream_auth: StreamAuthScope,
) -> AsyncIterator[StreamAuthScope]:
    async def session() -> AsyncIterator[AsyncSession]:
        async with stream_auth.sessions() as value:
            yield value

    stream_auth.app.dependency_overrides[get_session] = session
    yield stream_auth
    stream_auth.app.dependency_overrides.pop(get_session, None)


async def test_diff_uses_real_cookie_session_and_safe_repository_access(
    diff_session_auth: StreamAuthScope,
) -> None:
    scope = diff_session_auth
    await save_files(PostgresHarness(scope.engine), scope.review_id, [saved_file()])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=scope.app), base_url="http://test"
    ) as client:
        url = f"/api/v1/reviews/{scope.review_id}/diff"
        assert_error(await client.get(url), 401, "AUTHENTICATION_REQUIRED")
        client.cookies.set("dmc_session", "invalid-token")
        assert_error(await client.get(url), 401, "AUTHENTICATION_REQUIRED")
        client.cookies.set("dmc_session", scope.token)
        response = await client.get(url)
        assert response.status_code == HTTPStatus.OK
        assert_schema(response, "Diff")
        for id in ("invalid-uuid", str(uuid.uuid4())):
            assert_error(
                await client.get(f"/api/v1/reviews/{id}/diff"),
                404,
                "RESOURCE_NOT_FOUND",
            )
        async with scope.sessions.begin() as session:
            await session.execute(
                delete(models.RepositoryAccess).where(
                    models.RepositoryAccess.user_id == scope.user_id
                )
            )
        assert_error(await client.get(url), 404, "RESOURCE_NOT_FOUND")
        async with scope.sessions.begin() as session:
            await session.execute(
                update(models.AuthSession)
                .where(models.AuthSession.id == scope.session_id)
                .values(revoked_at=NOW)
            )
        assert_error(await client.get(url), 401, "AUTHENTICATION_REQUIRED")


async def test_cookie_diff_never_returns_partial_snapshot_during_retention(
    diff_session_auth: StreamAuthScope,
) -> None:
    scope = diff_session_auth
    harness = PostgresHarness(scope.engine)
    removed = await save_files(
        harness, scope.review_id, [saved_file(path="removed.py")]
    )
    await save_files(harness, scope.review_id, [saved_file(path="kept.py")])
    async with scope.sessions.begin() as session:
        await session.execute(
            insert(models.ChunkResult).values(
                id=uuid.uuid4(),
                review_job_id=scope.review_id,
                context_payload_id=removed,
                schema_version=1,
                summary="safe",
                limitations=[],
                latency_ms=1,
            )
        )
    async with scope.engine.connect() as connection:
        schema = await connection.scalar(text("SELECT current_schema()"))
    writer_engine = create_async_engine(
        scope.engine.url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    task: asyncio.Task[httpx.Response] | None = None
    try:
        async with (
            writer_engine.connect() as writer,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=scope.app),
                base_url="http://test",
                cookies={"dmc_session": scope.token},
            ) as client,
        ):
            transaction = await writer.begin()
            try:
                await writer.execute(
                    text("LOCK TABLE context_payload IN ACCESS EXCLUSIVE MODE")
                )
                await writer.execute(
                    delete(models.ContextPayload).where(
                        models.ContextPayload.id == removed
                    )
                )
                task = asyncio.create_task(
                    client.get(f"/api/v1/reviews/{scope.review_id}/diff")
                )
                async with asyncio.timeout(5):
                    while not await writer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE pg_backend_pid() = ANY(pg_blocking_pids(pid)))"
                        )
                    ):
                        await asyncio.sleep(0.01)
                await transaction.commit()
                response = await asyncio.wait_for(task, timeout=5)
            finally:
                if transaction.is_active:
                    await transaction.rollback()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        if response.status_code == HTTPStatus.OK:
            assert {file["path"] for file in response.json()["files"]} == {
                "kept.py",
                "removed.py",
            }
            assert_schema(response, "Diff")
        else:
            assert response.status_code == HTTPStatus.GONE
            assert response.json()["code"] == "DIFF_EXPIRED"
            assert_schema(response, "Error")
    finally:
        await writer_engine.dispose()
