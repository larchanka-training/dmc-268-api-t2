"""ContextPayload v1: the chunk contract between context builder and LLM gateway.

Mirrors SD §6 "Общий контракт". Unknown fields are rejected; line numbers start
at 1, ranges are inclusive and a missing side is ``None``, never ``0``.
"""

from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import FindingSide
from app.review.schemas.finding import RelPath


def _check_unique[T](items: list[T]) -> list[T]:
    if len(set(items)) != len(items):
        msg = "items must be unique"
        raise ValueError(msg)
    return items


NonEmptyStr = Annotated[str, Field(min_length=1)]
UniqueStrs = Annotated[list[NonEmptyStr], AfterValidator(_check_unique)]


def _check_order(start: int, end: int) -> None:
    if start > end:
        msg = "start_line must not exceed end_line"
        raise ValueError(msg)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class LineKind(StrEnum):
    ADDED = "added"
    DELETED = "deleted"
    CONTEXT = "context"


class SkipReason(StrEnum):
    POLICY = "policy"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    UNAVAILABLE_BLOB = "unavailable_blob"
    PROVIDER_TRUNCATED = "provider_truncated"
    TOKEN_BUDGET = "token_budget"
    FILE_LIMIT = "file_limit"
    LINE_LIMIT = "line_limit"
    CONTEXT_ERROR = "context_error"


class ContextQuality(StrEnum):
    FULL = "full"
    DEGRADED = "degraded"
    DIFF_ONLY = "diff_only"


class ProviderMetadata(_Frozen):
    pull_number: int = Field(ge=1)
    diff_version: NonEmptyStr


class Snapshot(_Frozen):
    provider: Literal["github"]
    base_repository_id: NonEmptyStr
    head_repository_id: NonEmptyStr
    base_sha: NonEmptyStr
    merge_base_sha: NonEmptyStr
    head_sha: NonEmptyStr
    captured_at: datetime
    provider_metadata: ProviderMetadata


class Discussion(_Frozen):
    """Existing PR discussion relevant to the chunk (shape not fixed by SD)."""

    author: str
    body: str
    path: RelPath | None = None
    line: int | None = Field(default=None, ge=1)


class Metadata(_Frozen):
    title: str
    body: str | None
    base_ref: NonEmptyStr
    head_ref: NonEmptyStr
    commit_messages: list[str]
    languages: UniqueStrs
    discussions: list[Discussion]
    rules_version: NonEmptyStr
    # New settings use "ru"; frozen historical versions keep their language.
    output_language: NonEmptyStr


class HunkLine(_Frozen):
    kind: LineKind
    text: str
    old_line: int | None = Field(ge=1)
    new_line: int | None = Field(ge=1)

    @model_validator(mode="after")
    def _sides_match_kind(self) -> Self:
        has_old, has_new = self.old_line is not None, self.new_line is not None
        expected = {
            LineKind.ADDED: (False, True),
            LineKind.DELETED: (True, False),
            LineKind.CONTEXT: (True, True),
        }[self.kind]
        if (has_old, has_new) != expected:
            msg = f"{self.kind} line has inconsistent old_line/new_line"
            raise ValueError(msg)
        return self


class Hunk(_Frozen):
    hunk_id: NonEmptyStr
    old_start: int = Field(ge=0)
    old_count: int = Field(ge=0)
    new_start: int = Field(ge=0)
    new_count: int = Field(ge=0)
    lines: list[HunkLine]

    @model_validator(mode="after")
    def _counts_match_lines(self) -> Self:
        old = sum(1 for line in self.lines if line.old_line is not None)
        new = sum(1 for line in self.lines if line.new_line is not None)
        if (old, new) != (self.old_count, self.new_count):
            msg = "hunk old_count/new_count do not match its lines"
            raise ValueError(msg)
        if (self.old_count and not self.old_start) or (
            self.new_count and not self.new_start
        ):
            msg = "a nonempty hunk side must start at line 1 or later"
            raise ValueError(msg)
        for side in ("old_line", "new_line"):
            numbers = [n for line in self.lines if (n := getattr(line, side))]
            if any(b <= a for a, b in pairwise(numbers)):
                msg = f"hunk {side} numbers must strictly increase"
                raise ValueError(msg)
        return self


class FileDiff(_Frozen):
    file_id: NonEmptyStr
    old_path: RelPath | None
    new_path: RelPath | None
    status: FileStatus
    old_blob_sha: str | None
    new_blob_sha: str | None
    language: NonEmptyStr | None
    hunks: list[Hunk]


class LineRange(_Frozen):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _check_order(self.start_line, self.end_line)
        return self


class SourceWindow(_Frozen):
    path: RelPath
    side: FindingSide
    commit_sha: NonEmptyStr
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str
    covered_hunk_ids: UniqueStrs

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _check_order(self.start_line, self.end_line)
        return self


class WholeFile(_Frozen):
    path: RelPath
    side: FindingSide
    commit_sha: NonEmptyStr
    text: str
    changed_ranges: list[LineRange]


class FileContext(_Frozen):
    diff: FileDiff
    source_windows: list[SourceWindow]
    whole_file: WholeFile | None


class RelatedSymbol(_Frozen):
    qualified_name: NonEmptyStr
    kind: NonEmptyStr
    signature: str
    path: RelPath
    commit_sha: NonEmptyStr
    range: LineRange
    relation: NonEmptyStr
    relation_to_change: NonEmptyStr
    source_excerpt: str


class CoveredFile(_Frozen):
    path: RelPath
    ranges: list[LineRange]


class SkippedItem(_Frozen):
    path: RelPath
    ranges: list[LineRange] = Field(default_factory=list)
    reason: SkipReason


class Coverage(_Frozen):
    included: list[CoveredFile]
    skipped: list[SkippedItem]
    context_quality: ContextQuality


class Budget(_Frozen):
    estimated_input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    limit_input_tokens: int = Field(ge=0)


class ContextPayload(_Frozen):
    schema_version: Literal[1]
    review_id: NonEmptyStr
    chunk_id: NonEmptyStr
    snapshot: Snapshot
    metadata: Metadata
    files: list[FileContext] = Field(min_length=1)
    related_symbols: list[RelatedSymbol]
    coverage: Coverage
    budget: Budget
