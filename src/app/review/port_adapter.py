"""`LlmGatewayPort` over `ReviewService` (docs/sprint2-contract-handoff.md §2).

The internal `ReviewChunkOutcome` is mapped to the canonical
`ReviewChunkResult` (schemas/review-result/v1.json). Fingerprints, status,
provider/model provenance and request counts stay backend-side; ``usage`` is
reported only when both token counts are known.
"""

from app.application.ports import (
    BuiltContextPayload,
    ReviewChunkResult,
    ReviewFinding,
    TokenUsage,
)
from app.llm.budget import RunBudget
from app.review.schemas.context import ContextPayload
from app.review.schemas.finding import ValidatedFinding
from app.review.schemas.result import ChunkStatus, ReviewChunkOutcome
from app.review.service import ReviewService

_SCHEMA_VERSION = 1


class ChunkReviewFailedError(Exception):
    """The chunk FAILED: the port result has no status, so failure is raised.

    ``outcome`` keeps the safe ``error_code`` and provenance for the caller.
    """

    def __init__(self, outcome: ReviewChunkOutcome) -> None:
        super().__init__(outcome.error_code)
        self.outcome = outcome


class LlmGatewayAdapter:
    """One instance per review run: ``budget`` is shared by its chunks."""

    def __init__(self, service: ReviewService, budget: RunBudget) -> None:
        self._service = service
        self._budget = budget

    async def review(self, payload: BuiltContextPayload) -> ReviewChunkResult:
        context = ContextPayload.model_validate(payload.to_wire())
        outcome = await self._service.review_chunk(context, self._budget)
        if outcome.status == ChunkStatus.FAILED:
            raise ChunkReviewFailedError(outcome)
        return to_port_result(outcome)


def to_port_result(outcome: ReviewChunkOutcome) -> ReviewChunkResult:
    usage = outcome.usage
    tokens = (
        TokenUsage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
        if usage.input_tokens is not None and usage.output_tokens is not None
        else None
    )
    return ReviewChunkResult(
        schema_version=_SCHEMA_VERSION,
        summary=outcome.summary or "",  # always set for COMPLETED
        limitations=list(outcome.limitations),
        usage=tokens,
        latency_ms=usage.latency_ms,
        findings=[_to_port_finding(f) for f in outcome.findings],
    )


def _to_port_finding(finding: ValidatedFinding) -> ReviewFinding:
    return ReviewFinding(
        path=finding.path,
        side=finding.side,
        start_line=finding.start_line,
        end_line=finding.end_line,
        category=finding.category,
        severity=finding.severity,
        title=finding.title,
        explanation=finding.explanation,
        evidence=finding.evidence,
        recommendation=finding.recommendation,
        proposed_diff_fix=finding.proposed_diff_fix,
    )
