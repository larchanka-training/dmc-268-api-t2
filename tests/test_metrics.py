"""Metrics are structured log events; labels never carry code or paths."""

import uuid

from structlog.testing import capture_logs

from app.core.metrics import count, record_skipped_files


def test_count_emits_one_metric_event_with_labels() -> None:
    with capture_logs() as events:
        count("llm_calls_total", 2, cache="exact")

    assert events == [
        {
            "event": "metric",
            "log_level": "info",
            "metric": "llm_calls_total",
            "value": 2,
            "cache": "exact",
        }
    ]


def test_skipped_files_are_counted_per_reason_without_paths() -> None:
    review_id = uuid.uuid4()
    skipped = [
        {"path": "a.lock", "ranges": [], "reason": "policy", "detail": "lockfile"},
        {"path": "b.lock", "ranges": [], "reason": "policy", "detail": "lockfile"},
        {"path": "vendor/x.py", "ranges": [], "reason": "policy"},
    ]

    with capture_logs() as events:
        record_skipped_files(review_id, skipped)

    assert sorted((e["reason"], e["value"]) for e in events) == [
        ("lockfile", 2),
        ("policy", 1),
    ]
    assert all(e["metric"] == "files_skipped_total" for e in events)
    assert all(e["review_id"] == str(review_id) for e in events)
    assert not any("path" in e or "a.lock" in str(e) for e in events)
