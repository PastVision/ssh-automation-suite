"""DST and schedule-time policy. These pin the exact instants from the design.

T1-T3 are the assertion of record: if an APScheduler upgrade changes how a
nonexistent local time is resolved, these fail loudly instead of silently
shifting every run by an hour.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from app.services import timezones as tz


def berlin():
    return tz.get_zone("Europe/Berlin")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_unknown_timezone_is_rejected():
    with pytest.raises(tz.ScheduleTimeError, match="IANA"):
        tz.validate_timezone("Mars/Olympus_Mons")


def test_cron_requires_five_fields():
    with pytest.raises(tz.ScheduleTimeError, match="5 fields"):
        tz.normalize_cron("* * *")


def test_cron_is_canonicalised():
    assert tz.normalize_cron("  0   2  *  *  * ") == "0 2 * * *"


def test_invalid_cron_is_rejected():
    with pytest.raises(tz.ScheduleTimeError):
        tz.normalize_cron("99 99 * * *")


def test_monthly_rejects_days_that_skip_months():
    with pytest.raises(tz.ScheduleTimeError, match="between 1 and 28"):
        tz.monthly_cron(time(2, 30), 31)


def test_weekly_rejects_out_of_range_weekday():
    with pytest.raises(tz.ScheduleTimeError):
        tz.weekly_cron(time(2, 30), 7)


# --------------------------------------------------------------------------- #
# T1/T2 - spring forward: the wall time does not exist
# --------------------------------------------------------------------------- #


def test_t1_spring_forward_keeps_the_utc_instant_and_reports_the_shift():
    trigger = tz.build_trigger(timezone="Europe/Berlin", cron_expression="30 2 * * *")
    # 03:00Z on the 28th is 04:00 local, after that day's 02:30.
    start = datetime(2026, 3, 28, 3, 0, tzinfo=UTC)

    firings = tz.upcoming_firings(trigger, start, "Europe/Berlin", count=2)

    # 2026-03-29 02:30 local does not exist: the gap is skipped forward.
    assert firings[0].scheduled_for == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert firings[0].local_time == datetime(2026, 3, 29, 3, 30, tzinfo=berlin())
    assert firings[0].shifted is True
    assert "daylight-saving" in (firings[0].detail or "")


def test_t2_normal_day_is_not_shifted():
    trigger = tz.build_trigger(timezone="Europe/Berlin", cron_expression="30 2 * * *")
    start = datetime(2026, 3, 27, 0, 0, tzinfo=UTC)

    firing = tz.upcoming_firings(trigger, start, "Europe/Berlin", count=1)[0]

    assert firing.scheduled_for == datetime(2026, 3, 27, 1, 30, tzinfo=UTC)
    assert firing.local_time == datetime(2026, 3, 27, 2, 30, tzinfo=berlin())
    assert firing.shifted is False
    assert firing.detail is None


# --------------------------------------------------------------------------- #
# T3/T4 - fall back: the wall time happens twice
# --------------------------------------------------------------------------- #


def test_t3_fall_back_produces_two_distinct_utc_occurrences():
    trigger = tz.build_trigger(timezone="Europe/Berlin", cron_expression="30 2 * * *")
    start = datetime(2026, 10, 24, 3, 0, tzinfo=UTC)

    firings = tz.upcoming_firings(trigger, start, "Europe/Berlin", count=4)
    on_the_25th = [f for f in firings if f.scheduled_for.date() == datetime(2026, 10, 25).date()]

    assert len(on_the_25th) == 2
    assert [f.scheduled_for for f in on_the_25th] == [
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
    ]
    # Both are real local 02:30s, so neither is a DST shift.
    assert all(f.shifted is False for f in on_the_25th)


def test_t4_fall_back_occurrences_are_distinct_ledger_keys():
    """The ledger is keyed on the UTC instant, so the two 02:30s never collide."""
    trigger = tz.build_trigger(timezone="Europe/Berlin", cron_expression="30 2 * * *")
    start = datetime(2026, 10, 24, 3, 0, tzinfo=UTC)
    keys = [f.scheduled_for for f in tz.upcoming_firings(
        trigger, start, "Europe/Berlin", count=4) if f.scheduled_for.day == 25]
    assert len(set(keys)) == 2


# --------------------------------------------------------------------------- #
# T6/T7 - zones with no DST, and the southern hemisphere
# --------------------------------------------------------------------------- #


def test_t6_half_hour_offset_zone_is_never_shifted():
    trigger = tz.build_trigger(timezone="Asia/Kolkata", cron_expression="30 2 * * *")
    start = datetime(2026, 3, 27, 0, 0, tzinfo=UTC)

    firings = tz.upcoming_firings(trigger, start, "Asia/Kolkata", count=3)

    assert all(f.shifted is False for f in firings)
    # +05:30 is preserved exactly.
    assert firings[0].scheduled_for == datetime(2026, 3, 27, 21, 0, tzinfo=UTC)
    assert firings[0].local_time.utcoffset().total_seconds() == 19800


def test_t7_southern_hemisphere_gap_is_in_october():
    trigger = tz.build_trigger(timezone="Australia/Sydney", cron_expression="30 2 * * *")
    # Sydney springs forward 2026-10-04 02:00 -> 03:00.
    start = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    firings = tz.upcoming_firings(trigger, start, "Australia/Sydney", count=4)

    assert any(f.shifted for f in firings), "October gap should be detected"
    assert firings[0].shifted is False


# --------------------------------------------------------------------------- #
# One-time schedules
# --------------------------------------------------------------------------- #


def test_one_time_trigger_fires_once_then_exhausts():
    run_at = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
    trigger = tz.build_trigger(timezone="UTC", run_at=run_at)

    first = tz.next_firing_after(trigger, datetime(2026, 11, 1, tzinfo=UTC), "UTC")
    assert first == run_at
    assert tz.next_firing_after(trigger, run_at, "UTC") is None


def test_naive_run_at_is_interpreted_in_the_schedule_zone():
    firing = tz.resolve_fire_time(datetime(2026, 7, 1, 12, 0), "Europe/Berlin")
    assert firing.scheduled_for == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Misfire scan
# --------------------------------------------------------------------------- #


def test_missed_firings_returns_every_occurrence_in_the_window():
    trigger = tz.build_trigger(timezone="UTC", cron_expression="30 2 * * *")
    result = tz.missed_firings(
        trigger,
        last_fired_for=datetime(2026, 3, 27, 2, 30, tzinfo=UTC),
        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        grace_seconds=300,
        timezone="UTC",
    )
    assert [f.scheduled_for for f in result.missed] == [
        datetime(2026, 3, 28, 2, 30, tzinfo=UTC),
        datetime(2026, 3, 29, 2, 30, tzinfo=UTC),
        datetime(2026, 3, 30, 2, 30, tzinfo=UTC),
    ]
    assert result.truncated is False


def test_missed_firings_is_empty_inside_the_grace_window():
    trigger = tz.build_trigger(timezone="UTC", cron_expression="0 * * * *")
    result = tz.missed_firings(
        trigger,
        last_fired_for=datetime(2026, 3, 27, 11, 0, tzinfo=UTC),
        now=datetime(2026, 3, 27, 12, 4, tzinfo=UTC),
        grace_seconds=300,
        timezone="UTC",
    )
    assert result.missed == []


def test_missed_firings_is_reproducible():
    """Same inputs must give the same list: that is what makes retry idempotent."""
    trigger = tz.build_trigger(timezone="UTC", cron_expression="*/15 * * * *")
    kwargs = dict(
        last_fired_for=datetime(2026, 3, 27, 10, 0, tzinfo=UTC),
        now=datetime(2026, 3, 27, 12, 0, tzinfo=UTC),
        grace_seconds=0,
        timezone="UTC",
    )
    first = tz.missed_firings(trigger, **kwargs).missed
    second = tz.missed_firings(trigger, **kwargs).missed
    assert [f.scheduled_for for f in first] == [f.scheduled_for for f in second]
    # Strictly after ``last_fired_for`` (10:00 already happened), up to and
    # including the cutoff (``now - grace``): 12:00 == now, so it is owed.
    assert len(first) == 8
    assert first[0].scheduled_for == datetime(2026, 3, 27, 10, 15, tzinfo=UTC)
    assert first[-1].scheduled_for == datetime(2026, 3, 27, 12, 0, tzinfo=UTC)
