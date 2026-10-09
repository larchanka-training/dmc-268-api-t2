"""Safe contract projection of persisted coverage."""

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

RepositoryPath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        pattern=re.compile(
            r"^(?!/)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*//)(?!.*\\)(?!.*\x00)(?!.*/$).+$"
        ),
    ),
]


class Projection(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class LineRange(Projection):
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("Reversed range")
        return self


class CoverageEntry(Projection):
    path: RepositoryPath
    ranges: list[LineRange]


class SkippedCoverageEntry(CoverageEntry):
    reason: Literal[
        "policy",
        "unsupported_language",
        "unavailable_blob",
        "provider_truncated",
        "token_budget",
        "file_limit",
        "line_limit",
        "context_error",
    ]


class Coverage(Projection):
    included: list[CoverageEntry]
    skipped: list[SkippedCoverageEntry]
    context_quality: Literal["full", "degraded", "diff_only"]
