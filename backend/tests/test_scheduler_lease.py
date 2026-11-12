"""Scheduler lease: acquisition, generation, renewal, and fencing.

These assert the guarantee stated in ``app.services.lease``: at most one live
holder, a superseded holder cannot renew, and fencing is a guarded *write* that a
stale holder cannot pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.session import get_session_factory
from app.core.config import get_settings
from app.services.lease import (
    SchedulerLease,
    read_lease,
)

LEASE_TTL_SECONDS = get_settings().scheduler_lease_seconds

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _lease_row():
    session = get_session_factory()()
    try:
        return session.execute(
            text("SELECT holder_id, generation, released_at FROM scheduler_lease WHERE id=1")
        ).first()
    finally:
        session.close()


def _force_expiry(generation: int) -> None:
    session = get_session_factory()()
    try:
        session.execute(
            text("UPDATE scheduler_lease SET expires_at = :past WHERE id = 1"),
            {"past": NOW - timedelta(seconds=1)},
        )
        session.commit()
    finally:
        session.close()


def test_first_acquisition_creates_generation_one(migrated_db):
    lease = SchedulerLease("proc-a")
    state = lease.acquire(now=NOW)

    assert state.held is True
    assert state.generation == 1
    assert lease.held is True
    assert _lease_row()[0] == "proc-a"


def test_second_process_cannot_take_a_live_lease(migrated_db):
    first = SchedulerLease("proc-a")
    assert first.acquire(now=NOW).held is True

    second = SchedulerLease("proc-b")
    state = second.acquire(now=NOW + timedelta(seconds=5))

    assert state.held is False
    assert state.reason == "lease_held_by_other"
    assert second.held is False
    # The original holder is untouched.
    assert _lease_row()[0] == "proc-a"


def test_expired_lease_is_claimable_and_generation_increments(migrated_db):
    first = SchedulerLease("proc-a")
    first.acquire(now=NOW)
    _force_expiry(1)

    second = SchedulerLease("proc-b")
    state = second.acquire(now=NOW + timedelta(seconds=LEASE_TTL_SECONDS + 1))

    assert state.held is True
    assert state.generation == 2
    assert _lease_row()[0] == "proc-b"


def test_superseded_holder_cannot_renew(migrated_db):
    first = SchedulerLease("proc-a")
    first.acquire(now=NOW)
    _force_expiry(1)

    second = SchedulerLease("proc-b")
    second.acquire(now=NOW + timedelta(seconds=LEASE_TTL_SECONDS + 1))

    # The old holder still believes it owns generation 1.
    assert first.renew(now=NOW + timedelta(seconds=LEASE_TTL_SECONDS + 2)) is False
    assert first.held is False
    assert first.state.reason == "lease_lost"
    # And it did not resurrect the row.
    assert _lease_row()[0] == "proc-b"


def test_current_holder_renews(migrated_db):
    lease = SchedulerLease("proc-a")
    lease.acquire(now=NOW)
    assert lease.renew(now=NOW + timedelta(seconds=10)) is True
    assert lease.held is True


def test_validate_and_touch_passes_for_the_owner(migrated_db):
    lease = SchedulerLease("proc-a")
    lease.acquire(now=NOW)

    session = get_session_factory()()
    try:
        assert lease.validate_and_touch(session, now=NOW) is True
        session.commit()
    finally:
        session.close()


def test_validate_and_touch_fails_for_a_superseded_holder(migrated_db):
    """This is the fence: a stale holder cannot pass the guarded write."""
    first = SchedulerLease("proc-a")
    first.acquire(now=NOW)
    _force_expiry(1)

    second = SchedulerLease("proc-b")
    second.acquire(now=NOW + timedelta(seconds=LEASE_TTL_SECONDS + 1))

    session = get_session_factory()()
    try:
        assert first.validate_and_touch(session, now=NOW) is False
        session.rollback()
    finally:
        session.close()
    assert first.held is False


def test_release_allows_immediate_reacquisition(migrated_db):
    first = SchedulerLease("proc-a")
    first.acquire(now=NOW)
    first.release(now=NOW)

    second = SchedulerLease("proc-b")
    state = second.acquire(now=NOW + timedelta(seconds=1))

    assert state.held is True
    assert state.generation == 2


def test_read_lease_reports_state_without_mutating(migrated_db):
    # No pinned time: ``read_lease`` compares against the real clock, so acquiring
    # at a fixed past instant would correctly look expired.
    lease = SchedulerLease("proc-a")
    lease.acquire()

    snapshot = read_lease()
    assert snapshot.held is True
    assert snapshot.holder_id == "proc-a"
    # Reading twice changes nothing.
    assert read_lease().generation == snapshot.generation
