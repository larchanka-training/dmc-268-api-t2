"""Domain validation of model findings (SD §7, §12; spec §8 step 4).

A valid JSON shape does not make a finding true: coordinates must hit changed
lines exactly and evidence must be present. Invalid findings are dropped and
counted, never moved to a neighbouring line.
"""

import hashlib
import re
from dataclasses import dataclass, field

from app.review.schemas.context import ContextPayload, Hunk, LineKind
from app.review.schemas.finding import LLMFinding, Side, ValidatedFinding
from app.review.schemas.result import ReviewChunkResult

# Baseline secret patterns (SD §12): AWS keys, GitHub tokens, password=..., PEM.
_SECRET_PATTERNS = (
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"), "***"),
    (
        re.compile(r"(?i)\b(password|passwd|pwd|secret|api_key|token)(\s*[=:]\s*)\S+"),
        r"\1\2***",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "***",
    ),
)
_TEXT_FIELDS = ("title", "explanation", "evidence", "recommendation")

_CHANGED_KIND = {Side.NEW: LineKind.ADDED, Side.OLD: LineKind.DELETED}


@dataclass(frozen=True)
class ValidationOutcome:
    accepted: list[ValidatedFinding] = field(default_factory=list)
    rejected_count: int = 0


def fingerprint(finding: LLMFinding) -> str:
    """Hash of normalized path/side/range/category/title (SD §7)."""
    loc = finding.location
    title = _normalize(finding.title)
    key = "|".join(
        [
            loc.path,
            loc.side,
            str(loc.line),
            str(loc.end_line or loc.line),
            finding.category,
            title,
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class FindingValidator:
    def validate(
        self, findings: list[LLMFinding], payload: ContextPayload
    ) -> ValidationOutcome:
        accepted: list[ValidatedFinding] = []
        rejected = 0
        for finding in findings:
            if not _hits_changed_lines(finding, payload) or not _has_evidence(finding):
                rejected += 1
                continue
            masked = finding.model_copy(
                update={
                    name: mask_secrets(getattr(finding, name)) for name in _TEXT_FIELDS
                }
            )
            accepted.append(
                ValidatedFinding(**masked.model_dump(), fingerprint=fingerprint(masked))
            )
        return ValidationOutcome(
            accepted=merge_findings([accepted]), rejected_count=rejected
        )


def merge_findings(groups: list[list[ValidatedFinding]]) -> list[ValidatedFinding]:
    """Keep the first finding per fingerprint, preserving order."""
    seen: set[str] = set()
    merged: list[ValidatedFinding] = []
    for group in groups:
        for finding in group:
            if finding.fingerprint not in seen:
                seen.add(finding.fingerprint)
                merged.append(finding)
    return merged


def merge_chunk_results(results: list[ReviewChunkResult]) -> list[ValidatedFinding]:
    """Run-level dedup across chunks (spec §8); the run summary is the caller's."""
    return merge_findings([result.findings for result in results])


def mask_secrets(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _has_evidence(finding: LLMFinding) -> bool:
    evidence = _normalize(finding.evidence)
    return bool(evidence) and evidence != _normalize(finding.title)


def _hits_changed_lines(finding: LLMFinding, payload: ContextPayload) -> bool:
    """``line`` is a changed line of its side; the whole ``line..end_line``
    range lies inside the same hunk (context lines allowed after the start)."""
    loc = finding.location
    end = loc.end_line or loc.line
    for file in payload.files:
        diff = file.diff
        path = diff.new_path if loc.side == Side.NEW else diff.old_path
        if path != loc.path:
            continue
        for hunk in diff.hunks:
            numbers = _side_lines(hunk, loc.side)
            if numbers.get(loc.line) == _CHANGED_KIND[loc.side] and all(
                n in numbers for n in range(loc.line, end + 1)
            ):
                return True
    return False


def _side_lines(hunk: Hunk, side: Side) -> dict[int, LineKind]:
    attr = "new_line" if side == Side.NEW else "old_line"
    return {
        number: line.kind
        for line in hunk.lines
        if (number := getattr(line, attr)) is not None
    }
