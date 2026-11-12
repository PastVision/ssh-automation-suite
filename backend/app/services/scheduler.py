"""The scheduler loop: walk each schedule's cursor forward, dispatch what is due.

The design is deliberately one loop over one persisted cursor
(``Schedule.next_run_at``) rather than a separate "scan" and "recover" pass:

* ``next_run_at`` is the only source of truth for what is due. Recovery after
  downtime is therefore not a special mode -- it is simply the loop handling
  several due instants in a row, because the cursor is still behind.
* Every due instant is decided exactly once and then recorded on the firing
  ledger, keyed by ``(schedule_id, scheduled_for)`` under a UNIQUE constraint. A
  cycle that crashes halfway leaves the remaining instants plainly due again, and
  a replay of the one that was in flight collides on the ledger instead of running
  twice.
* Each schedule's work per cycle is bounded, so a schedule that has been behind
  for months cannot turn one tick into a dispatch storm. Truncation is reported,
  not swallowed.

Lateness applies the misfire policy: an instant at or before ``now - grace`` is
late, and is skipped or collapsed to the newest of the burst depending on the
schedule's policy, instead of being run late. That is a decision about a *set* of
instants, which is why it lives here and not in ``dispatch``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.db.types import as_utc
from app.models.schedule import (
    MisfirePolicy,
    Schedule,
    ScheduleKind,
    ScheduleRunOutcome,
    ScheduleState,
)
from app.services import dispatch as dispatch_service
from app.services import schedules as schedule_service
from app.services.lease import SchedulerLease
from app.services.timezones import build_trigger, resolve_fire_time

logger = logging.getLogger(__name__)


@dataclass
class CycleReport:
    """What one cycle did. Returned so tests assert on behaviour, not on logs."""

    handled: list[tuple[int, datetime]] = field(default_factory=list)
    truncated: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        return not (self.handled or self.failed)


@dataclass
class SchedulerStatus:
    """Whether this process is dispatching, for the health endpoint."""

    running: bool
    lease_held: bool
    generation: int | None = None


class Scheduler:
    """Owns the lease and the loop. One instance per process."""

    def __init__(
        self,
        *,
        lease: SchedulerLease | None = None,
        now: datetime | None = None,
    ) -> None:
        self.settings = get_settings()
        self.lease = lease or SchedulerLease()
        # Tests pin the clock; production reads it per cycle.
        self._now_override = now
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_report = CycleReport()

    @property
    def last_report(self) -> CycleReport:
        return self._last_report

    def status(self) -> SchedulerStatus:
        """Cheap, side-effect-free view of whether this process is dispatching.

        ``running`` is the loop task, ``lease_held`` is the last known arbitration
        outcome. They differ while a second process is idle waiting for the lease,
        which is a normal state and not a fault.
        """
        return SchedulerStatus(
            running=self._task is not None and not self._task.done(),
            lease_held=self.lease.held,
            generation=self.lease.generation,
        )

    def _now(self) -> datetime:
        return self._now_override or datetime.now(UTC)

    # -- lifecycle --------------------------------------------------------- #

    async def run_forever(self) -> None:
        """Acquire the lease, then tick until cancelled or superseded."""
        state = self.lease.acquire(now=self._now())
        if not state.held:
            # Not an error: another process owns scheduling. Stay idle rather than
            # exiting so this process can take over if the holder disappears.
            logger.info("Scheduler idle: %s", state.description)
        else:
            logger.info("Scheduler acquired the lease (generation %s)", state.generation)
            self._reconcile_abandoned()

        try:
            while not self._stopping.is_set():
                if not self.lease.held:
                    if self.lease.acquire(now=self._now()).held:
                        logger.info("Scheduler acquired a vacant lease")
                        # The previous holder died without settling its work; the
                        # lease changing hands is exactly the evidence needed.
                        self._reconcile_abandoned()
                if self.lease.held:
                    await self.tick()
                await self._sleep(self.settings.scheduler_poll_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                self.lease.release(now=self._now())

    def _reconcile_abandoned(self) -> None:
        """Settle scheduled executions left non-terminal by a dead holder.

        Without this, the overlap index turns one crash into a schedule that never
        fires again: the abandoned row keeps its PENDING/RUNNING status forever and
        every later firing is refused as an overlap. Runs this process is executing
        are excluded, because re-acquiring our own lapsed lease is not evidence
        that our work died.
        """
        from app.services import execution_runner, execution_state

        in_flight: set[int] = set()
        if execution_runner.RUNNER_SINGLETON is not None:
            # Defensive: a test double or an alternative runner need not implement
            # the introspection method, and recovery must still run without it.
            active = getattr(execution_runner.get_runner(), "active_execution_ids", None)
            if callable(active):
                in_flight = set(active())
        try:
            settled = execution_state.reconcile_abandoned_executions(
                exclude=in_flight,
                reason="the process running it stopped unexpectedly",
            )
        except Exception:  # noqa: BLE001 - never let recovery stop scheduling
            logger.exception("Could not reconcile abandoned executions")
            return
        if settled:
            logger.warning(
                "Settled %d abandoned scheduled execution(s): %s",
                len(settled),
                settled,
            )

    def request_stop(self) -> None:
        self._stopping.set()

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping.clear()
        self._task = asyncio.create_task(self.run_forever(), name="scheduler")
        return self._task

    async def stop(self) -> None:
        self.request_stop()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    # -- one cycle --------------------------------------------------------- #

    async def tick(self) -> CycleReport:
        """One poll cycle. Per-schedule failures are recorded, never raised."""
        report = CycleReport()
        now = self._now()

        if not self.lease.renew(now=now):
            logger.warning("Scheduler lost the lease; stopping dispatch")
            self._stopping.set()
            return report

        for schedule in self._live_schedules():
            try:
                self._process(schedule, now=now, report=report)
            except Exception as exc:  # noqa: BLE001 - one bad schedule must not stop the loop
                logger.exception("Schedule %s failed during a cycle", schedule.id)
                report.failed.append((schedule.id, str(exc)))

        self._last_report = report
        return report

    def _live_schedules(self) -> list[Schedule]:
        session = get_session_factory()()
        try:
            rows = session.scalars(
                select(Schedule).where(
                    Schedule.deleted_at.is_(None),
                    Schedule.enabled.is_(True),
                )
            ).all()
            # Detach so the objects stay usable after the session closes.
            for row in rows:
                session.expunge(row)
            return [row for row in rows if not row.is_terminal]
        finally:
            session.close()

    def _process(self, schedule: Schedule, *, now: datetime, report: CycleReport) -> None:
        if schedule.schedule_kind == ScheduleKind.ONCE:
            self._process_once(schedule, now=now, report=report)
            return
        self._process_recurring(schedule, now=now, report=report)

    # -- one-time ---------------------------------------------------------- #

    def _process_once(self, schedule: Schedule, *, now: datetime, report: CycleReport) -> None:
        """A one-time schedule fires at exactly one instant, then is terminal."""
        if schedule.run_at is None:
            self._retire(schedule, ScheduleState.MISSED, "The schedule has no run time.")
            return

        run_at = as_utc(schedule.run_at)
        if run_at > now:
            return

        if now - run_at > timedelta(seconds=schedule.misfire_grace_seconds):
            if schedule.misfire_policy is MisfirePolicy.SKIP:
                dispatch_service.record_skipped(
                    schedule.id,
                    [run_at],
                    detail=(
                        "This run was missed while the service was down and the "
                        "schedule's misfire policy is to skip missed runs."
                    ),
                )
                report.handled.append((schedule.id, run_at))
                self._retire(
                    schedule,
                    ScheduleState.MISSED,
                    "The scheduled time passed while the service was down.",
                )
                return

        result = dispatch_service.dispatch(schedule.id, run_at, lease=self.lease)
        report.handled.append((schedule.id, run_at))
        if result.dispatched:
            # ``record_firing`` derives the terminal state from the ledger outcome,
            # so a successful one-time run ends as COMPLETED rather than MISSED.
            schedule_service.record_firing(
                schedule.id, scheduled_for=run_at, next_run_at=None, dispatched=True
            )
            return
        if result.outcome is ScheduleRunOutcome.ERROR:
            report.failed.append((schedule.id, result.detail or "error"))
        # Distinguish "the instant passed without running" from "the run was refused
        # for a reason that will never resolve". Reporting a broken workflow as
        # MISSED would be a lie: nothing was missed, the definition is unusable.
        unusable = result.outcome in {
            ScheduleRunOutcome.INVALID_WORKFLOW,
            ScheduleRunOutcome.UNAUTHORIZED,
            ScheduleRunOutcome.SKIPPED_OWNER_INACTIVE,
            ScheduleRunOutcome.ERROR,
        }
        self._retire(
            schedule,
            ScheduleState.FAILED if unusable else ScheduleState.MISSED,
            result.detail or "The scheduled run could not start.",
        )

    # -- recurring --------------------------------------------------------- #

    def _process_recurring(
        self, schedule: Schedule, *, now: datetime, report: CycleReport
    ) -> None:
        """Handle every instant that has come due since the cursor, then persist it."""
        try:
            trigger = self._trigger(schedule)
        except Exception:
            logger.exception("Schedule %s has an unusable trigger", schedule.id)
            self._retire(
                schedule, ScheduleState.PAUSED, "The schedule's timing is invalid."
            )
            return

        cursor = self._cursor(schedule, trigger, now=now)
        if cursor is None or cursor > now:
            return

        limit = self.settings.scheduler_max_catchup_per_cycle
        due = _due_instants(
            trigger,
            cursor=cursor,
            now=now,
            timezone=schedule.timezone,
            limit=limit + 1,
        )
        if len(due) > limit:
            # The schedule is further behind than one cycle may resolve. Handle the
            # **oldest** instants and leave the cursor behind, so the remainder is
            # still plainly due next cycle. Taking the newest instead would strand
            # the ones in the middle: the probe only proved that at least one more
            # exists, not how many.
            due = due[:limit]
            report.truncated.append(schedule.id)
        if not due:
            return

        grace = timedelta(seconds=schedule.misfire_grace_seconds)
        late = [instant for instant in due if instant < now - grace]
        on_time = [instant for instant in due if instant >= now - grace]

        if late:
            if schedule.misfire_policy is MisfirePolicy.SKIP:
                dispatch_service.record_skipped(
                    schedule.id,
                    late,
                    detail=(
                        "This firing was missed while the service was down and the "
                        "schedule's misfire policy is to skip missed firings."
                    ),
                )
                report.handled.extend((schedule.id, instant) for instant in late)
            else:
                # RUN_ONCE: collapse the burst to its newest, but record the rest so
                # "why did it not run at 03:00?" still has an answer.
                collapsed, newest = late[:-1], late[-1]
                if collapsed:
                    dispatch_service.record_skipped(
                        schedule.id,
                        collapsed,
                        detail=(
                            "This firing was missed while the service was down. The "
                            "schedule's misfire policy runs only the most recent "
                            "missed firing, so this one was skipped."
                        ),
                    )
                    report.handled.extend((schedule.id, instant) for instant in collapsed)
                on_time.insert(0, newest)

        for instant in on_time:
            result = dispatch_service.dispatch(schedule.id, instant, lease=self.lease)
            report.handled.append((schedule.id, instant))
            if result.outcome.value == "error":
                report.failed.append((schedule.id, result.detail or "error"))
            elif result.outcome.value in {"invalid_workflow", "unauthorized"}:
                # The workflow is gone or inaccessible; every later instant would
                # fail the same way, so stop and record why.
                self._retire(
                    schedule, ScheduleState.PAUSED, result.detail or result.outcome
                )
                return

        self._advance(schedule, trigger, through=due[-1])

    def _cursor(self, schedule: Schedule, trigger, *, now: datetime) -> datetime | None:
        """The next instant this schedule owes.

        ``next_run_at`` normally holds it. When it is absent -- a row whose first
        computation never ran, or one resumed without a cursor -- it is derived from
        the trigger so the schedule cannot silently stall.
        """
        if schedule.next_run_at is not None:
            return as_utc(schedule.next_run_at)
        try:
            computed = _next_after(trigger, after=now, timezone=schedule.timezone)
        except Exception:
            logger.exception("Schedule %s could not compute a cursor", schedule.id)
            return None
        schedule_service.set_next_run_at(schedule.id, computed)
        return computed

    def _advance(self, schedule: Schedule, trigger, *, through: datetime) -> None:
        try:
            next_at = _next_after(trigger, after=through, timezone=schedule.timezone)
        except Exception:
            logger.exception("Schedule %s could not compute its next firing", schedule.id)
            self._retire(
                schedule, ScheduleState.PAUSED, "The schedule's trigger became invalid."
            )
            return
        schedule_service.record_firing(
            schedule.id, scheduled_for=through, next_run_at=next_at, dispatched=True
        )

    def _trigger(self, schedule: Schedule):
        return build_trigger(
            timezone=schedule.timezone,
            cron_expression=schedule.cron_expression,
            run_at=schedule.run_at,
        )

    def _retire(self, schedule: Schedule, state: ScheduleState, reason: str) -> None:
        schedule_service.mark_terminal(schedule.id, state, reason)


# --------------------------------------------------------------------------- #
# Trigger arithmetic
# --------------------------------------------------------------------------- #

def _due_instants(
    trigger, *, cursor: datetime, now: datetime, timezone: str, limit: int
) -> list[datetime]:
    """Firings from ``cursor`` (inclusive) through ``now``, oldest first.

    ``cursor`` is itself a firing, so the probe steps back one second to include it
    while still making progress past it. The loop is bounded by ``limit``.
    """
    out: list[datetime] = []
    probe = cursor - timedelta(seconds=1)
    previous = None
    while len(out) < limit:
        raw = trigger.get_next_fire_time(previous, probe)
        if raw is None:
            break
        resolved = resolve_fire_time(raw, timezone)
        if resolved.scheduled_for > now:
            break
        out.append(resolved.scheduled_for)
        previous = raw
        probe = resolved.scheduled_for
    return out


def _next_after(trigger, *, after: datetime, timezone: str) -> datetime | None:
    """The first firing strictly after ``after``.

    ``BaseTrigger.get_next_fire_time`` is *inclusive* of its ``now`` argument, so
    asking from ``after`` returns ``after`` itself when that instant is a firing.
    Probing from that candidate with it as ``previous`` is what steps past it; the
    loop is bounded because each round either moves forward or gives up.
    """
    probe = after
    previous = None
    for _ in range(3):
        raw = trigger.get_next_fire_time(previous, probe)
        if raw is None:
            return None
        resolved = resolve_fire_time(raw, timezone)
        if resolved.scheduled_for > after:
            return resolved.scheduled_for
        previous = raw
        probe = resolved.scheduled_for
    return None


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


def peek_scheduler() -> Scheduler | None:
    """The process-wide scheduler if it exists, without creating one."""
    return _scheduler


def reset_scheduler() -> None:
    """Test hook: drop the process-wide scheduler."""
    global _scheduler
    _scheduler = None


def scheduler_status() -> SchedulerStatus:
    """The process's scheduler state, without creating a scheduler.

    The health endpoint must not spawn one as a side effect, so this reads the
    singleton only if something already built it.
    """
    existing = peek_scheduler()
    if existing is None:
        return SchedulerStatus(running=False, lease_held=False)
    return existing.status()

