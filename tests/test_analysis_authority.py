"""Deterministic rolling lease timing; seconds advance without sleeping."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.application.analysis_authority import LiveAnalysisAuthority
from app.application.ports import LeaseLostError, TaskLeaseSnapshot


def test_confirmed_15_second_renewals_keep_120_second_http_authorized() -> None:
    now = [0.0]
    db_now = datetime(2026, 10, 15, tzinfo=UTC)
    lease = TaskLeaseSnapshot(
        uuid.uuid4(), "worker", db_now + timedelta(seconds=90), 1, "analyze", db_now
    )
    authority = LiveAnalysisAuthority(lease, started=0, clock=lambda: now[0])
    for seconds in range(15, 121, 15):
        now[0] = seconds
        authority.confirm(
            replace(
                lease,
                sampled_at=db_now + timedelta(seconds=seconds),
                expires_at=db_now + timedelta(seconds=seconds + 90),
            ),
            started=seconds,
        )
        authority.check()
    now[0] = 211
    with pytest.raises(LeaseLostError):
        authority.check()


def test_delayed_confirmation_cannot_revive_expired_execution() -> None:
    now = [0.0]
    db_now = datetime(2026, 10, 15, tzinfo=UTC)
    lease = TaskLeaseSnapshot(
        uuid.uuid4(), "worker", db_now + timedelta(seconds=90), 1, "analyze", db_now
    )
    authority = LiveAnalysisAuthority(lease, started=0, clock=lambda: now[0])
    now[0] = 91
    with pytest.raises(LeaseLostError):
        authority.confirm(
            replace(lease, expires_at=db_now + timedelta(seconds=180)), started=15
        )


def test_lease_without_db_clock_sample_cannot_authorize_io() -> None:
    db_now = datetime(2099, 10, 15, tzinfo=UTC)
    lease = TaskLeaseSnapshot(
        uuid.uuid4(), "worker", db_now + timedelta(seconds=90), 1, "analyze"
    )
    with pytest.raises(LeaseLostError, match="clock sample"):
        LiveAnalysisAuthority(lease, started=0, clock=lambda: 0)
