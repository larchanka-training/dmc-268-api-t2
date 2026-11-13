"""Exact review result cache in Redis (dmc-268-ui-t2#33).

The key hashes the reviewed diff without line numbers (hunks relative to the
first hunk of their file, lines as kind and right-trimmed text), the file
paths, the output language and the frozen ``config_digest`` (model, prompt,
rules and settings version): another model, prompt or rule set misses by key,
never by manual purge. PR title, body and identities are not part of the key.

Findings are stored relative to their file's first hunk and restored at the
current lines. Redis is optional: every cache failure is logged and counted,
and the review proceeds as if uncached (fail-open). Single-flight: one LLM call
per key at a time; others wait for its result, then fall back to calling.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.application.ports import ReviewChunkResult, ReviewFinding, TokenUsage
from app.core.metric_events import count
from app.domain.enums import FindingCategory, FindingSeverity, FindingSide

# path -> (old side, new side) start of the file's first hunk
Anchors = dict[str, tuple[int, int]]


class ReviewCachePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key_version: int = Field(default=1, ge=1)
    ttl_seconds: int = Field(default=14 * 24 * 3600, ge=1)
    # Longest expected LLM call; a crashed holder's lock expires after it.
    lock_ttl_seconds: int = Field(default=180, ge=1)
    wait_seconds: float = Field(default=30.0, ge=0)
    poll_interval_seconds: float = Field(default=0.5, gt=0)


def _path(diff: Mapping[str, Any]) -> str:
    return str(diff["new_path"] or diff["old_path"])


def cache_identity(
    body: Mapping[str, Any], config_digest: str, *, version: int = 1
) -> tuple[str, Anchors]:
    anchors: Anchors = {}
    files = []
    for file in body["files"]:
        diff = file["diff"]
        hunks = diff["hunks"]
        old0, new0 = (hunks[0]["old_start"], hunks[0]["new_start"]) if hunks else (0, 0)
        # A finding on the OLD side of a renamed file carries old_path; on a
        # same-named file both keys coincide. A current new_path wins a clash.
        if diff["old_path"]:
            anchors.setdefault(str(diff["old_path"]), (old0, new0))
        anchors[_path(diff)] = (old0, new0)
        files.append(
            {
                "old_path": diff["old_path"],
                "new_path": diff["new_path"],
                "status": diff["status"],
                "language": diff["language"],
                "hunks": [
                    [
                        hunk["old_start"] - old0,
                        hunk["old_count"],
                        hunk["new_start"] - new0,
                        hunk["new_count"],
                        [
                            [line["kind"], line["text"].rstrip()]
                            for line in hunk["lines"]
                        ],
                    ]
                    for hunk in hunks
                ],
                "source_windows": file["source_windows"],
                "whole_file": file["whole_file"],
            }
        )
    identity = {
        "config_digest": config_digest,
        "output_language": body["metadata"].get("output_language"),
        "files": files,
        "related_symbols": body["related_symbols"],
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"review:cache:v{version}:{hashlib.sha256(encoded).hexdigest()}", anchors


def _shift(line: int, side: str, anchor: tuple[int, int], sign: int) -> int:
    return line + sign * (anchor[0] if side == FindingSide.OLD else anchor[1])


def to_cached(result: ReviewChunkResult, anchors: Anchors) -> dict[str, Any]:
    findings = []
    for finding in result.findings:
        anchor = anchors[finding.path]
        values = asdict(finding)
        for key in ("start_line", "end_line"):
            values[key] = _shift(values[key], finding.side, anchor, -1)
        findings.append(values)
    return {
        "schema_version": result.schema_version,
        "summary": result.summary,
        "limitations": list(result.limitations),
        "usage": asdict(result.usage) if result.usage is not None else None,
        "findings": findings,
    }


def from_cached(value: Mapping[str, Any], anchors: Anchors) -> ReviewChunkResult | None:
    findings = []
    for item in value["findings"]:
        anchor = anchors.get(item["path"])
        if anchor is None:
            return None
        side = FindingSide(item["side"])
        findings.append(
            ReviewFinding(
                **{
                    **item,
                    "side": side,
                    "category": FindingCategory(item["category"]),
                    "severity": FindingSeverity(item["severity"]),
                    "start_line": _shift(item["start_line"], side, anchor, 1),
                    "end_line": _shift(item["end_line"], side, anchor, 1),
                }
            )
        )
    return ReviewChunkResult(
        schema_version=value["schema_version"],
        summary=value["summary"],
        limitations=list(value["limitations"]),
        usage=TokenUsage(0, 0),
        latency_ms=0,
        findings=findings,
    )


def _saved_tokens(value: Mapping[str, Any]) -> int:
    usage = value.get("usage") or {}
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


class ReviewResultCache:
    def __init__(self, redis: Redis, policy: ReviewCachePolicy) -> None:
        self._redis = redis
        self._policy = policy

    async def review(
        self,
        body: Mapping[str, Any],
        config_digest: str,
        call: Callable[[], Awaitable[ReviewChunkResult]],
        valid: Callable[[ReviewChunkResult], bool],
    ) -> ReviewChunkResult:
        """Cached result, else ``call``; only ``valid`` results are stored."""
        key, anchors = cache_identity(
            body, config_digest, version=self._policy.key_version
        )
        hit = await self._lookup(key, anchors, valid)
        if hit is not None:
            return hit
        token = secrets.token_hex(16)
        holder = await self._acquire(key, token)
        if holder is False:
            hit = await self._wait(key, anchors, valid)
            if hit is not None:
                return hit
        count("cache_miss", kind="exact")
        try:
            result = await call()
            if valid(result):
                await self._store(key, to_cached(result, anchors))
            return result
        finally:
            if holder:
                await self._release(key, token)

    async def _lookup(
        self,
        key: str,
        anchors: Anchors,
        valid: Callable[[ReviewChunkResult], bool],
    ) -> ReviewChunkResult | None:
        try:
            raw = await self._redis.get(key)
        except RedisError as error:
            self._unavailable("get", error)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            restored = from_cached(value, anchors)
        except ValueError, KeyError, TypeError:
            restored = None
        if restored is None or not valid(restored):
            count("cache_rejected", kind="exact")
            return None
        count("cache_hit", kind="exact")
        count("llm_tokens_saved_total", _saved_tokens(value), kind="exact")
        structlog.get_logger().info("review_cache_hit", key=key, kind="exact")
        return restored

    async def _acquire(self, key: str, token: str) -> bool | None:
        """True: this call holds the in-flight lock; False: another does;
        None: Redis unavailable, call without coordination."""
        try:
            acquired = await self._redis.set(
                f"{key}:lock", token, nx=True, ex=self._policy.lock_ttl_seconds
            )
        except RedisError as error:
            self._unavailable("lock", error)
            return None
        return bool(acquired)

    async def _wait(
        self,
        key: str,
        anchors: Anchors,
        valid: Callable[[ReviewChunkResult], bool],
    ) -> ReviewChunkResult | None:
        deadline = asyncio.get_running_loop().time() + self._policy.wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self._policy.poll_interval_seconds)
            hit = await self._lookup(key, anchors, valid)
            if hit is not None:
                return hit
            try:
                if not await self._redis.exists(f"{key}:lock"):
                    return None  # the holder finished without a cacheable result
            except RedisError as error:
                self._unavailable("wait", error)
                return None
        return None

    async def _store(self, key: str, value: dict[str, Any]) -> None:
        try:
            await self._redis.set(
                key, json.dumps(value, ensure_ascii=False), ex=self._policy.ttl_seconds
            )
        except RedisError as error:
            self._unavailable("set", error)

    async def _release(self, key: str, token: str) -> None:
        try:
            if await self._redis.get(f"{key}:lock") in {token, token.encode()}:
                await self._redis.delete(f"{key}:lock")
        except RedisError as error:
            self._unavailable("unlock", error)

    @staticmethod
    def _unavailable(operation: str, error: RedisError) -> None:
        count("cache_error", kind="exact", operation=operation)
        structlog.get_logger().warning(
            "review_cache_unavailable",
            operation=operation,
            error_type=type(error).__name__,
        )
