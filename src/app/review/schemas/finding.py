"""Finding vocabulary shared by the model output and the context contract."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _check_relative_path(path: str) -> str:
    parts = path.split("/")
    if not path or path.startswith("/") or "\\" in path or ".." in parts:
        msg = "path must be relative, normalized and use '/' separators"
        raise ValueError(msg)
    if any(part in {"", "."} for part in parts):
        msg = "path must be normalized"
        raise ValueError(msg)
    return path


RelPath = Annotated[str, AfterValidator(_check_relative_path)]


class Side(StrEnum):
    OLD = "OLD"  # deleted lines: old-side coordinates
    NEW = "NEW"  # added/context lines: new-side coordinates


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"  # "info" is not a v1 finding (SD §7, §14)


class Category(StrEnum):
    SECURITY = "security"
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"  # style/formatting is not a category


class _Strict(BaseModel):
    # No defaults on purpose: OpenAI strict mode needs every property required.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Location(_Strict):
    path: RelPath
    side: Side
    line: int = Field(ge=1)
    end_line: int | None = Field(ge=1)  # same side, end_line >= line

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_line is not None and self.end_line < self.line:
            msg = "end_line must not be less than line"
            raise ValueError(msg)
        return self


class LLMFinding(_Strict):
    location: Location
    category: Category
    severity: Severity
    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=1000)
    recommendation: str = Field(min_length=1, max_length=2000)


class ValidatedFinding(LLMFinding):
    """LLMFinding accepted by the domain validator, with a backend fingerprint."""

    fingerprint: str
