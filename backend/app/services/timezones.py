"""Schedule time arithmetic, including the DST policy.

Everything persisted is a **UTC instant**. A schedule stores an IANA timezone
name and, for recurring schedules, a normalised 5-field cron expression; local
wall-clock arithmetic happens only here.

Why this module exists rather than calling ``CronTrigger`` directly: APScheduler
3.x resolves a nonexistent local time by keeping the wall time and attaching the
offset that was in force before the transition. For ``02:30`` on 2026-03-29 in
``Europe/Berlin`` it returns ``02:30+01:00``, whose real UTC instant is
``01:30Z`` -- but that instant is genuinely ``03:30+02:00`` local. The **UTC
instant is the correct one to keep** and the local *label* is wrong.

The approved policy is therefore: trust the UTC instant, discard the label, and
report the shift. ``resolve_fire_time`` is the only place that decides this, so
an APScheduler upgrade that changes the behaviour fails the DST tests loudly
instead of silently moving runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

# A schedule that has been disabled for years must not produce an unbounded
# catch-up list. The scan stops and reports truncation instead.
MAX_CATCHUP_SCAN = 10_000

CRON_FIELDS = 5

# APScheduler 3.x numbers ``day_of_week`` Monday=0, while a crontab expression
# numbers it Sunday=0 (and accepts 7 as Sunday). This is a documented upstream
# "historical mistake" that cannot be fixed in the 3.x series, so a user's
# ``0 9 * * 1`` meaning Monday would silently fire on Tuesday. The weekday field is
# therefore translated explicitly, in both directions, and nothing else in the
# codebase calls ``CronTrigger.from_crontab``.
_WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_CRON_NAME_TO_SCHEDULER = {
    "sun": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
}


class ScheduleTimeError(ValueError):
    """Invalid timezone, cron expression, or one-time instant."""


@dataclass(frozen=True)
class ResolvedFiring:
    """One logical firing: the instant plus how its wall time was interpreted."""

    scheduled_for: datetime
    local_time: datetime
    shifted: bool

    @property
    def detail(self) -> str | None:
        if not self.shifted:
            return None
        return (
            "The configured local time does not exist on this date because of a "
            f"daylight-saving transition; it ran at {self.local_time:%H:%M} instead."
        )


def get_zone(name: str) -> ZoneInfo:
    """Resolve an IANA name, rejecting anything the platform cannot load."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleTimeError(
            f"Unknown timezone {name!r}. Use an IANA name such as 'Europe/Berlin'."
        ) from exc


def validate_timezone(name: str) -> str:
    get_zone(name)
    return name


def _translate_weekday(field: str) -> str:
    """Rewrite a crontab weekday field into APScheduler's Monday=0 numbering.

    Handles the forms a user actually writes: ``*``, ``1``, ``7``, ``mon``,
    ``1-5``, ``mon-fri``, ``1,3,5``, ``*/2``, and ``5-6/2``. Anything else is passed
    through unchanged so ``CronTrigger`` can raise its own, more precise error.
    """
    text = field.strip()
    if not text or text == "*":
        return text

    def one(token: str) -> str:
        lowered = token.strip().lower()
        if lowered in _CRON_NAME_TO_SCHEDULER:
            return str(_CRON_NAME_TO_SCHEDULER[lowered])
        try:
            value = int(lowered)
        except ValueError:
            return lowered
        if value == 7:  # crontab accepts 7 as a second spelling of Sunday
            value = 0
        if not 0 <= value <= 6:
            return lowered
        return str((value + 6) % 7)

    out: list[str] = []
    for part in text.split(","):
        body, sep, step = part.partition("/")
        if "-" in body:
            start, _, end = body.partition("-")
            body = f"{one(start)}-{one(end)}"
        elif body not in {"*", ""}:
            body = one(body)
        out.append(f"{body}{sep}{step}" if sep else body)
    return ",".join(out)


def crontab_trigger(expression: str, zone: ZoneInfo) -> CronTrigger:
    """Build a ``CronTrigger`` from a standard crontab expression.

    The only place weekday numbering is translated, so a caller cannot
    accidentally reintroduce the APScheduler 3.x off-by-one.
    """
    text = " ".join(expression.split())
    if not text:
        raise ScheduleTimeError("A cron expression is required.")
    fields = text.split(" ")
    if len(fields) != CRON_FIELDS:
        raise ScheduleTimeError(
            "A cron expression must have exactly 5 fields "
            "(minute hour day-of-month month day-of-week)."
        )
    minute, hour, day, month, weekday = fields
    try:
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=_translate_weekday(weekday),
            timezone=zone,
        )
    except (ValueError, TypeError) as exc:
        raise ScheduleTimeError(f"Invalid cron expression: {exc}") from exc


def normalize_cron(expression: str) -> str:
    """Validate and canonicalise a 5-field crontab expression."""
    crontab_trigger(expression, UTC)
    return " ".join(expression.split())


def daily_cron(at: time) -> str:
    return f"{at.minute} {at.hour} * * *"


def weekly_cron(at: time, weekday: int) -> str:
    """``weekday`` is 0=Sunday..6=Saturday, matching crontab's own numbering."""
    if not 0 <= weekday <= 6:
        raise ScheduleTimeError("weekday must be between 0 (Sunday) and 6 (Saturday).")
    return f"{at.minute} {at.hour} * * {weekday}"


def monthly_cron(at: time, day_of_month: int) -> str:
    """Days 1-28 only: 29-31 would silently skip months that are too short."""
    if not 1 <= day_of_month <= 28:
        raise ScheduleTimeError(
            "day_of_month must be between 1 and 28 so every month has that day."
        )
    return f"{at.minute} {at.hour} {day_of_month} * *"


def build_trigger(
    *,
    timezone: str,
    cron_expression: str | None = None,
    run_at: datetime | None = None,
) -> BaseTrigger:
    """Build the trigger for a schedule. ``run_at`` wins when present."""
    zone = get_zone(timezone)
    if run_at is not None:
        return DateTrigger(run_date=_as_aware(run_at, zone))
    if cron_expression is None:
        raise ScheduleTimeError("A recurring schedule needs a cron expression.")
    try:
        return crontab_trigger(cron_expression, zone)
    except ScheduleTimeError:
        raise
    except (ValueError, TypeError) as exc:  # pragma: no cover - guarded by normalize
        raise ScheduleTimeError(f"Invalid cron expression: {exc}") from exc


def _as_aware(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value


def resolve_fire_time(raw: datetime, timezone: str) -> ResolvedFiring:
    """Map a trigger's output to (UTC instant, was the wall time skipped?).

    The UTC instant is authoritative. ``shifted`` is True when the instant's real
    local time differs from the wall time the trigger reported, i.e. the
    configured local time did not exist that day.
    """
    zone = get_zone(timezone)
    aware = _as_aware(raw, zone)
    instant = aware.astimezone(UTC)
    local = instant.astimezone(zone)
    shifted = (local.hour, local.minute) != (aware.hour, aware.minute) or (
        local.date() != aware.date()
    )
    return ResolvedFiring(scheduled_for=instant, local_time=local, shifted=shifted)


def next_firing_after(trigger: BaseTrigger, after: datetime, timezone: str) -> datetime | None:
    """The first logical firing strictly after ``after``, normalised to UTC.

    ``DateTrigger`` ignores the ``now`` argument and always reports its date, so a
    one-time schedule must be compared explicitly or a used-up schedule would look
    perpetually pending.
    """
    cursor = _as_aware(after, get_zone(timezone))
    nxt = trigger.get_next_fire_time(None, cursor)
    if nxt is None:
        return None
    resolved = resolve_fire_time(nxt, timezone)
    if resolved.scheduled_for <= cursor:
        return None
    return resolved.scheduled_for


def upcoming_firings(
    trigger: BaseTrigger, after: datetime, timezone: str, *, count: int
) -> list[ResolvedFiring]:
    """The next ``count`` firings, which is what the UI previews."""
    out: list[ResolvedFiring] = []
    cursor = _as_aware(after, get_zone(timezone))
    previous = None
    while len(out) < count:
        nxt = trigger.get_next_fire_time(previous, cursor)
        if nxt is None:
            break
        resolved = resolve_fire_time(nxt, timezone)
        if resolved.scheduled_for <= cursor:
            break
        out.append(resolved)
        # ``previous`` is the last emitted firing and ``cursor`` the search floor;
        # advancing both is what lets a duplicated local hour report twice.
        previous = nxt
        cursor = resolved.scheduled_for
    return out


@dataclass(frozen=True)
class MissedFirings:
    """Logical firings that fell inside the grace window while we were down."""

    missed: list[ResolvedFiring]
    truncated: bool = False


def missed_firings(
    trigger: BaseTrigger,
    *,
    last_fired_for: datetime | None,
    now: datetime,
    grace_seconds: int,
    timezone: str,
) -> MissedFirings:
    """Every logical firing in ``(last_fired_for, now - grace]``, oldest first.

    Deducing this from the schedule's own trigger makes the result reproducible:
    recomputing after a crash yields exactly the same list, which is what lets the
    ledger's unique constraint absorb a retried firing instead of running it twice.
    """
    grace = max(0, grace_seconds)
    cutoff = now - _seconds(grace)
    zone = get_zone(timezone)

    cursor = _as_aware(last_fired_for, zone) if last_fired_for else None
    if cursor is None:
        # No recorded firing yet: the schedule's own start is the lower bound, so
        # a brand-new schedule does not owe every firing since creation.
        cursor = cutoff

    out: list[ResolvedFiring] = []
    probe = cursor
    # ``last_fired_for`` is already accounted for: seeding ``previous`` with the
    # cursor makes the first step strictly after it, so a recorded firing is never
    # replayed and the grace boundary is excluded.
    previous = cursor
    while True:
        nxt = trigger.get_next_fire_time(previous, probe)
        if nxt is None:
            return MissedFirings(missed=out)
        resolved = resolve_fire_time(nxt, timezone)
        if resolved.scheduled_for > cutoff:
            return MissedFirings(missed=out)
        out.append(resolved)
        previous = nxt
        probe = resolved.scheduled_for
        if len(out) >= MAX_CATCHUP_SCAN:
            return MissedFirings(missed=out, truncated=True)


def _seconds(value: int):
    return timedelta(seconds=value)
