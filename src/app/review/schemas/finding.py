"""Finding vocabulary shared by the model output and the context contract."""

from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import FindingCategory, FindingSeverity, FindingSide


def _check_relative_path(path: str) -> str:
    parts = path.split("/")
    unsafe = "\\" in path or "\0" in path or ".." in parts
    if not path or path.startswith("/") or unsafe:
        msg = "path must be relative, normalized and use '/' separators"
        raise ValueError(msg)
    if any(part in {"", "."} for part in parts):
        msg = "path must be normalized"
        raise ValueError(msg)
    return path


RelPath = Annotated[str, AfterValidator(_check_relative_path)]


class _Strict(BaseModel):
    # No defaults on purpose: OpenAI strict mode needs every property required.
    model_config = ConfigDict(extra="forbid", frozen=True)


class LLMFindingV1(_Strict):
    """Flat finding of ``schemas/review-result/v1.json#/$defs/finding``."""

    path: RelPath
    side: FindingSide
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)  # same side; equals start_line for one line
    category: FindingCategory  # style/formatting is not a category
    severity: FindingSeverity  # "info" is not a v1 finding (SD §7, §14)
    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=1000)
    recommendation: str = Field(min_length=1, max_length=2000)
    proposed_diff_fix: str | None = Field(max_length=4000)  # never auto-applied

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_line < self.start_line:
            msg = "end_line must not be less than start_line"
            raise ValueError(msg)
        return self


class LLMFinding(LLMFindingV1):
    """Provider v2 finding; the nullable rule reference is required on the wire."""

    # The validator rejects labels absent from the rules actually sent.
    rule_ref: str | None = Field(max_length=32)


class ValidatedFinding(LLMFinding):
    """LLMFinding accepted by the domain validator, with a backend fingerprint."""

    fingerprint: str
