"""Map a review cycle to the GitHub commit status that gates the merge."""

from dataclasses import dataclass
from typing import Literal

CommitState = Literal["pending", "success", "failure", "error"]

_ACTIVE = frozenset({"QUEUED", "FETCHING_DIFF", "PARSING_CONTEXT", "LLM_PROCESSING"})


@dataclass(frozen=True, slots=True)
class CommitStatusDecision:
    state: CommitState
    description: str


def commit_status_for(
    review_status: str, *, critical_findings: int
) -> CommitStatusDecision:
    """Only critical findings fail the check; a failed review is an error."""
    if review_status in _ACTIVE:
        return CommitStatusDecision("pending", "AI-ревью выполняется")
    if review_status == "FAILED":
        return CommitStatusDecision("error", "AI-ревью не выполнено")
    if critical_findings > 0:
        return CommitStatusDecision(
            "failure", f"Критических замечаний: {critical_findings}"
        )
    if review_status == "SKIPPED":
        return CommitStatusDecision("success", "Нет изменений для AI-ревью")
    return CommitStatusDecision("success", "Критических замечаний нет")
