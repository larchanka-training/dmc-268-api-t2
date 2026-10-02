"""Domain validation of model findings (SD §7, §12; spec §8 step 4)."""

from typing import Any

import pytest

from app.review.schemas.context import ContextPayload
from app.review.schemas.finding import LLMFinding
from app.review.validation import FindingValidator, merge_findings


@pytest.fixture
def payload(payload_data: dict[str, Any]) -> ContextPayload:
    """src/review.py hunk: context 38/38, deleted old 39, added new 39,
    context 40/40, added new 41."""
    hunk = payload_data["files"][0]["diff"]["hunks"][0]
    hunk["lines"] += [
        {"kind": "context", "text": "", "old_line": 40, "new_line": 40},
        {"kind": "added", "text": "# done", "old_line": None, "new_line": 41},
    ]
    hunk.update(old_count=3, new_count=4)
    return ContextPayload.model_validate(payload_data)


def _finding(**overrides: Any) -> LLMFinding:
    data: dict[str, Any] = {
        "location": {
            "path": "src/review.py",
            "side": "NEW",
            "line": 39,
            "end_line": None,
        },
        "category": "correctness",
        "severity": "high",
        "title": "Ошибка валидации не обрабатывается",
        "explanation": "ValidationError роняет обработчик.",
        "evidence": "Строка 39 вызывает model_validate без обработки ошибок.",
        "recommendation": "Перехватить ValidationError.",
    }
    data.update(overrides)
    return LLMFinding.model_validate(data)


def test_finding_on_a_changed_line_is_accepted_with_fingerprint(
    payload: ContextPayload,
) -> None:
    outcome = FindingValidator().validate([_finding()], payload)

    assert outcome.rejected_count == 0
    assert len(outcome.accepted) == 1
    accepted = outcome.accepted[0]
    assert accepted.title == "Ошибка валидации не обрабатывается"
    assert len(accepted.fingerprint) == 64  # noqa: PLR2004


def _at(path: str, side: str, line: int, end_line: int | None = None) -> LLMFinding:
    return _finding(
        location={"path": path, "side": side, "line": line, "end_line": end_line}
    )


@pytest.mark.parametrize(
    "finding",
    [
        _at("src/review.py", "NEW", 41),
        _at("src/review.py", "OLD", 39),
        _at("src/review.py", "NEW", 39, 41),  # spans context line 40
        _at("src/review.py", "NEW", 39, 40),
    ],
)
def test_coordinates_on_changed_lines_are_accepted(
    payload: ContextPayload, finding: LLMFinding
) -> None:
    outcome = FindingValidator().validate([finding], payload)

    assert (len(outcome.accepted), outcome.rejected_count) == (1, 0)


@pytest.mark.parametrize(
    "finding",
    [
        _at("src/other.py", "NEW", 39),  # file is not in the chunk
        _at("src/review.py", "NEW", 38),  # starts on a context line
        _at("src/review.py", "NEW", 40),
        _at("src/review.py", "OLD", 38),
        _at("src/review.py", "OLD", 40),
        _at("src/review.py", "NEW", 42),  # outside the diff
        _at("src/review.py", "NEW", 39, 45),  # runs past the hunk
    ],
)
def test_coordinates_off_changed_lines_are_rejected(
    payload: ContextPayload, finding: LLMFinding
) -> None:
    outcome = FindingValidator().validate([finding], payload)

    assert (outcome.accepted, outcome.rejected_count) == ([], 1)


def test_evidence_repeating_the_title_is_rejected(payload: ContextPayload) -> None:
    finding = _finding(title="Нет обработки", evidence="  нет   обработки ")

    outcome = FindingValidator().validate([finding], payload)

    assert (outcome.accepted, outcome.rejected_count) == ([], 1)


@pytest.mark.parametrize(
    ("leak", "secret"),
    [
        ("ключ AKIAIOSFODNN7EXAMPLE в коде", "AKIAIOSFODNN7EXAMPLE"),
        ("токен ghp_" + "a" * 36, "ghp_" + "a" * 36),
        ("строка password=hunter2 в конфиге", "hunter2"),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
            "MIIEow",
        ),
    ],
)
def test_secret_like_values_are_masked(
    payload: ContextPayload, leak: str, secret: str
) -> None:
    finding = _finding(explanation=leak, evidence=f"Строка 39: {leak}")

    accepted = FindingValidator().validate([finding], payload).accepted[0]

    assert secret not in accepted.explanation
    assert secret not in accepted.evidence
    assert "***" in accepted.explanation


def test_exact_duplicates_in_a_chunk_are_merged(payload: ContextPayload) -> None:
    duplicate = _finding(title="ошибка  ВАЛИДАЦИИ не обрабатывается")

    outcome = FindingValidator().validate([_finding(), duplicate], payload)

    assert len(outcome.accepted) == 1
    assert outcome.rejected_count == 0


def test_merge_chunk_results_deduplicates_across_chunks(
    payload: ContextPayload,
) -> None:
    validator = FindingValidator()
    first = validator.validate([_finding(), _at("src/review.py", "NEW", 41)], payload)
    second = validator.validate([_finding()], payload)

    merged = merge_findings([first.accepted, second.accepted])

    assert [f.location.line for f in merged] == [39, 41]
