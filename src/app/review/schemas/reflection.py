"""Reflection Pass verdict schema (`schemas/reflection-output` semantics).

The verifier answers per finding: keep or drop, with a machine-readable
reason. It never rewrites findings and cannot invent new ones.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.review.schemas.finding import _Strict

_VERDICT_REASONS: dict[str, frozenset[str]] = {
    "confirmed": frozenset({"verified"}),
    "rejected": frozenset({"no_real_consequence", "pre_existing", "overflagged_style"}),
}


class ReflectionVerdict(_Strict):
    """One finding's fate; ``index`` points into the findings list sent for
    verification (deterministic checks have already filtered the rest)."""

    index: int = Field(ge=0)
    verdict: Literal["confirmed", "rejected"]
    reason: Literal[
        "verified", "no_real_consequence", "pre_existing", "overflagged_style"
    ]

    @model_validator(mode="after")
    def _reason_matches_verdict(self) -> ReflectionVerdict:
        if self.reason not in _VERDICT_REASONS[self.verdict]:
            msg = f"reason {self.reason!r} does not fit verdict {self.verdict!r}"
            raise ValueError(msg)
        return self


class ReflectionOutput(BaseModel):
    """The only schema passed to the verifier as structured-output format."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdicts: list[ReflectionVerdict] = Field(max_length=50)

    @model_validator(mode="after")
    def _indexes_are_unique(self) -> ReflectionOutput:
        indexes = [verdict.index for verdict in self.verdicts]
        if len(set(indexes)) != len(indexes):
            msg = "verdicts must reference each finding index at most once"
            raise ValueError(msg)
        return self
