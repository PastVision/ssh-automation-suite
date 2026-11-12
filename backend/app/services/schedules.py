"""Schedule CRUD and the small state transitions the scheduler needs.

Authorization reuses the workflow module's rules rather than inventing a second
scheme: a schedule is only as visible as the workflow it runs. That means a caller
who may not see a workflow is told its schedule does not exist, and a caller who
may not edit it cannot schedule it.

Two deliberate asymmetries with the rest of the app:

* ``delete`` is a **soft** delete. A schedule is live infrastructure whose ledger
  explains past executions, so erasing the row would erase that answer.
* ``record_firing`` and ``mark_terminal`` are called by the scheduler, not by a
  request. They take no principal because there is no authenticated caller, and
  they only touch columns the scheduler owns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.db.types import as_utc
from app.models.audit_actions import AuditAction
from app.models.permission import ResourceType
from app.models.schedule import (
    MisfirePolicy,
    Schedule,
    ScheduleKind,
    ScheduleRun,
    ScheduleState,
)
from app.models.workflow import Workflow
from app.services import audit, permissions
from app.services.sessions import Principal

logger = logging.getLogger(__name__)

MAX_NAME = 120
MAX_GRACE_SECONDS = 86400


class ScheduleNotFound(LookupError):
    """No such schedule, or the caller may not see the workflow behind it."""


class ScheduleWorkflowMissing(LookupError):
    """The target workflow does not exist, or the caller may not see it.

    Distinct from ``ScheduleNotFound`` because it answers a different question, and
    the routes report both as 404 for the same reason: a caller must not be able to
    tell an invisible workflow from an absent one.
    """


class ScheduleConflict(RuntimeError):
    """The name is taken, or the edit is not coherent."""


class ScheduleInvalid(ValueError):
    """The submitted schedule body is not usable."""


@dataclass(frozen=True)
class ScheduleData:
    """Validated, storage-ready values. Built by the API layer."""

    name: str
    workflow_id: int
    schedule_kind: ScheduleKind
    timezone: str
    cron_expression: str | None = None
    run_at: datetime | None = None
    misfire_policy: MisfirePolicy = MisfirePolicy.SKIP
    misfire_grace_seconds: int = 300


def list_schedules(
    *, principal: Principal, workflow_id: int | None = None, include_deleted: bool = False
) -> list[Schedule]:
    """Schedules this caller may see, newest first."""
    session = get_session_factory()()
    try:
        statement = select(Schedule)
        if not include_deleted:
            statement = statement.where(Schedule.deleted_at.is_(None))
        if workflow_id is not None:
            statement = statement.where(Schedule.workflow_id == workflow_id)
        rows = session.scalars(statement.order_by(Schedule.created_at.desc())).all()

        visible: list[Schedule] = []
        for row in rows:
            if _may_view(session, row, principal.user_id):
                visible.append(row)
        for row in visible:
            session.expunge(row)
        return visible
    finally:
        session.close()


def get_schedule(schedule_id: int, *, principal: Principal) -> Schedule:
    session = get_session_factory()()
    try:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None or not _may_view(session, schedule, principal.user_id):
            raise ScheduleNotFound(schedule_id)
        session.expunge(schedule)
        return schedule
    finally:
        session.close()


def create(
    data: ScheduleData, *, principal: Principal, remote_addr: str | None = None
) -> Schedule:
    session = get_session_factory()()
    try:
        workflow = _load_workflow(session, data.workflow_id, principal)
        permissions.require_edit(
            ResourceType.WORKFLOW, workflow.id, workflow.owner_id, principal.user_id
        )
        _validate(data)

        schedule = Schedule(
            owner_id=principal.user_id,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            name=data.name.strip(),
            schedule_kind=data.schedule_kind,
            cron_expression=data.cron_expression,
            run_at=as_utc(data.run_at) if data.run_at else None,
            timezone=data.timezone,
            misfire_policy=data.misfire_policy,
            misfire_grace_seconds=data.misfire_grace_seconds,
            state=(
                ScheduleState.SCHEDULED
                if data.schedule_kind is ScheduleKind.ONCE
                else ScheduleState.ACTIVE
            ),
            enabled=True,
        )
        schedule.next_run_at = _first_run_at(data)
        session.add(schedule)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise ScheduleConflict(
                f"You already have an active schedule named {data.name!r}."
            ) from exc

        audit.record(
            AuditAction.SCHEDULE_CREATED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            resource_type=ResourceType.WORKFLOW.value,
            resource_id=workflow.id,
            remote_addr=remote_addr,
            detail={
                "schedule_id": schedule.id,
                "name": schedule.name,
                "kind": str(schedule.schedule_kind),
                "timezone": schedule.timezone,
                "next_run_at": schedule.next_run_at,
            },
            session=session,
        )
        session.commit()
        session.expunge(schedule)
        return schedule
    finally:
        session.close()


def update(
    schedule_id: int,
    data: ScheduleData,
    *,
    principal: Principal,
    remote_addr: str | None = None,
    expected_row_version: int | None = None,
) -> Schedule:
    """Replace the schedule's definition. Firing history is untouched.

    ``expected_row_version`` is compared before any write so two editors cannot
    silently overwrite each other; the scheduler also bumps the version every time
    it fires, so a client editing from a stale copy is told rather than obeyed.
    """
    session = get_session_factory()()
    try:
        schedule = _load(session, schedule_id, principal, need_edit=True)
        if schedule.deleted_at is not None:
            raise ScheduleNotFound(schedule_id)
        _require_version(schedule, expected_row_version)
        _validate(data)

        workflow = _load_workflow(session, data.workflow_id, principal)
        permissions.require_edit(
            ResourceType.WORKFLOW, workflow.id, workflow.owner_id, principal.user_id
        )

        schedule.name = data.name.strip()
        schedule.workflow_id = workflow.id
        schedule.workflow_name = workflow.name
        schedule.schedule_kind = data.schedule_kind
        schedule.cron_expression = data.cron_expression
        schedule.run_at = as_utc(data.run_at) if data.run_at else None
        schedule.timezone = data.timezone
        schedule.misfire_policy = data.misfire_policy
        schedule.misfire_grace_seconds = data.misfire_grace_seconds
        schedule.next_run_at = _first_run_at(data)

        # An edit re-arms a completed one-time schedule, because the user has just
        # supplied a new run time; without this the row would stay terminal.
        if schedule.schedule_kind is ScheduleKind.ONCE and not schedule.is_terminal:
            schedule.state = ScheduleState.SCHEDULED
        elif not schedule.is_terminal:
            schedule.state = (
                ScheduleState.ACTIVE if schedule.enabled else ScheduleState.PAUSED
            )
        schedule.row_version = (schedule.row_version or 0) + 1

        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise ScheduleConflict(
                f"You already have an active schedule named {data.name!r}."
            ) from exc

        audit.record(
            AuditAction.SCHEDULE_UPDATED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            resource_type=ResourceType.WORKFLOW.value,
            resource_id=schedule.workflow_id,
            remote_addr=remote_addr,
            detail={"schedule_id": schedule.id, "next_run_at": schedule.next_run_at},
            session=session,
        )
        session.commit()
        session.expunge(schedule)
        return schedule
    finally:
        session.close()


def delete(schedule_id: int, *, principal: Principal, remote_addr: str | None = None) -> None:
    """Soft delete. The ledger must survive the schedule it explains."""
    session = get_session_factory()()
    try:
        schedule = _load(session, schedule_id, principal, need_edit=True)
        if schedule.deleted_at is not None:
            return
        schedule.deleted_at = datetime.now(UTC)
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.state = ScheduleState.DISABLED
        schedule.row_version = (schedule.row_version or 0) + 1

        audit.record(
            AuditAction.SCHEDULE_DELETED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            resource_type=ResourceType.WORKFLOW.value,
            resource_id=schedule.workflow_id,
            remote_addr=remote_addr,
            detail={"schedule_id": schedule.id, "name": schedule.name},
            session=session,
        )
        session.commit()
    finally:
        session.close()


def set_enabled(
    schedule_id: int,
    enabled: bool,
    *,
    principal: Principal,
    remote_addr: str | None = None,
    expected_row_version: int | None = None,
) -> Schedule:
    """Pause or resume. Resuming recomputes the next firing from now."""
    session = get_session_factory()()
    try:
        schedule = _load(session, schedule_id, principal, need_edit=True)
        if schedule.deleted_at is not None:
            raise ScheduleNotFound(schedule_id)
        _require_version(schedule, expected_row_version)
        if schedule.is_terminal and enabled:
            raise ScheduleConflict(
                "This one-time schedule has already finished; edit its run time to "
                "schedule it again."
            )

        schedule.enabled = enabled
        schedule.state = (
            (ScheduleState.SCHEDULED if schedule.schedule_kind is ScheduleKind.ONCE
             else ScheduleState.ACTIVE)
            if enabled
            else ScheduleState.PAUSED
        )
        schedule.next_run_at = _first_run_at_from(schedule) if enabled else None
        schedule.row_version = (schedule.row_version or 0) + 1

        audit.record(
            AuditAction.SCHEDULE_RESUMED if enabled else AuditAction.SCHEDULE_PAUSED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            resource_type=ResourceType.WORKFLOW.value,
            resource_id=schedule.workflow_id,
            remote_addr=remote_addr,
            detail={"schedule_id": schedule.id},
            session=session,
        )
        session.commit()
        session.expunge(schedule)
        return schedule
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Scheduler-facing transitions: no principal, columns owned by the scheduler
# --------------------------------------------------------------------------- #

def record_firing(
    schedule_id: int,
    *,
    scheduled_for: datetime,
    next_run_at: datetime | None,
    dispatched: bool,
) -> None:
    """Advance a recurring schedule's cursor after a firing was handled.

    ``last_fired_for`` moves only here, and only after the firing has been decided.
    Recovery relies on that: an instant that was never handled stays outside the
    "already done" boundary and can still be replayed.

    ``dispatched`` distinguishes "this ran" from "this instant was decided and
    skipped". Both advance the cursor -- otherwise a failing schedule would retry
    the same instant forever -- but only a real run completes a one-time schedule.
    """
    session = get_session_factory()()
    try:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            return
        schedule.last_fired_for = as_utc(scheduled_for)
        schedule.next_run_at = as_utc(next_run_at) if next_run_at else None
        if schedule.schedule_kind is ScheduleKind.ONCE:
            if dispatched:
                schedule.state = ScheduleState.COMPLETED
                schedule.paused_reason = None
            elif next_run_at is None:
                # Decided and never running again. The ledger holds the detail;
                # the schedule itself is simply finished.
                schedule.state = ScheduleState.MISSED
            schedule.enabled = False
        schedule.row_version = (schedule.row_version or 0) + 1
        session.commit()
    except Exception:  # pragma: no cover - the ledger already recorded the outcome
        session.rollback()
        logger.exception("Could not advance schedule %s", schedule_id)
    finally:
        session.close()


def set_next_run_at(schedule_id: int, next_run_at: datetime | None) -> None:
    """Persist a derived cursor. Used only when ``next_run_at`` was missing."""
    session = get_session_factory()()
    try:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            return
        schedule.next_run_at = as_utc(next_run_at) if next_run_at else None
        session.commit()
    except Exception:  # pragma: no cover
        session.rollback()
        logger.exception("Could not set the cursor on schedule %s", schedule_id)
    finally:
        session.close()


def mark_terminal(schedule_id: int, state: ScheduleState, reason: str) -> None:
    """Retire a schedule so it is never scanned again, recording why.

    ``PAUSED`` means "stopped for a reason the user can fix" (the workflow became
    invalid or inaccessible); the other terminal states mean the schedule is done.
    Both clear ``next_run_at`` and ``enabled``, which is what takes it out of the
    scheduler's scan.
    """
    from app.models.schedule import PausedReason

    session = get_session_factory()()
    try:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            return
        if state is ScheduleState.PAUSED:
            schedule.state = ScheduleState.PAUSED
            schedule.paused_reason = _paused_reason(reason, PausedReason)
        else:
            schedule.state = state
            schedule.paused_reason = None
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.row_version = (schedule.row_version or 0) + 1
        session.commit()
    except Exception:  # pragma: no cover
        session.rollback()
        logger.exception("Could not retire schedule %s", schedule_id)
    finally:
        session.close()


def _paused_reason(reason: str, enum_cls) -> object | None:
    """Map the failure text back to the enum, so the UI can explain the pause."""
    for candidate in enum_cls:
        if candidate.value in reason:
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load(
    session: Session, schedule_id: int, principal: Principal, *, need_edit: bool
) -> Schedule:
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise ScheduleNotFound(schedule_id)
    # ``workflow_id`` is NULL once the workflow is deleted (the FK sets it null so
    # the name snapshot and the ledger survive). A schedule with no workflow is not
    # editable, but it must still be *readable* by its owner -- otherwise the
    # history of a deleted workflow's schedule becomes unreachable.
    if schedule.workflow_id is None:
        if need_edit:
            raise ScheduleConflict(
                "This schedule's workflow was deleted, so the schedule can no longer "
                "be changed."
            )
        if schedule.owner_id != principal.user_id:
            raise ScheduleNotFound(schedule_id)
        return schedule
    workflow = session.get(Workflow, schedule.workflow_id)
    if workflow is None:
        raise ScheduleNotFound(schedule_id)
    check = permissions.can_edit if need_edit else permissions.can_view
    if not check(ResourceType.WORKFLOW, workflow.id, workflow.owner_id, principal.user_id):
        # Deliberately "not found": revealing that a schedule exists on a workflow
        # the caller cannot see would leak the workflow's existence.
        raise ScheduleNotFound(schedule_id)
    return schedule


def _load_workflow(session: Session, workflow_id: int, principal: Principal) -> Workflow:
    workflow = session.get(Workflow, workflow_id)
    # Workflows are hard-deleted, so "missing" is the whole check. There is no
    # tombstone to consult.
    if workflow is None:
        raise ScheduleWorkflowMissing(workflow_id)
    if not permissions.can_view(
        ResourceType.WORKFLOW, workflow.id, workflow.owner_id, principal.user_id
    ):
        # Same answer as an absent workflow: otherwise this endpoint reveals which
        # workflow ids exist.
        raise ScheduleWorkflowMissing(workflow_id)
    return workflow


def _may_view(session: Session, schedule: Schedule, user_id: int) -> bool:
    if schedule.workflow_id is None:
        # The workflow was deleted, so no permission row can decide this. The
        # owner keeps access to their own schedule's history; nobody else does.
        return schedule.owner_id == user_id
    workflow = session.get(Workflow, schedule.workflow_id)
    if workflow is None:
        return schedule.owner_id == user_id
    return permissions.can_view(
        ResourceType.WORKFLOW, workflow.id, workflow.owner_id, user_id
    )


def _require_version(schedule: Schedule, expected: int | None) -> None:
    """Optimistic concurrency guard.

    ``None`` means "the caller did not send a version", which is allowed so the
    service stays usable from scripts. ``schedule.row_version`` moves on every edit
    *and* on every firing, so a stale client is refused rather than silently
    clobbering a change it never saw.
    """
    if expected is not None and expected != schedule.row_version:
        raise ScheduleConflict(
            "This schedule changed since you loaded it. Reload it and try again."
        )


def _validate(data: ScheduleData) -> None:
    name = data.name.strip()
    if not name:
        raise ScheduleInvalid("A schedule needs a name.")
    if len(name) > MAX_NAME:
        raise ScheduleInvalid(f"A schedule name may be at most {MAX_NAME} characters.")
    if not 0 <= data.misfire_grace_seconds <= MAX_GRACE_SECONDS:
        raise ScheduleInvalid("The misfire grace period is out of range.")
    if data.schedule_kind is ScheduleKind.ONCE:
        if data.run_at is None:
            raise ScheduleInvalid("A one-time schedule needs a run time.")
        if data.cron_expression is not None:
            raise ScheduleInvalid("A one-time schedule cannot have a cron expression.")
    else:
        if not data.cron_expression:
            raise ScheduleInvalid("A recurring schedule needs a cron expression.")
        if data.run_at is not None:
            raise ScheduleInvalid("Only a one-time schedule may have a run time.")


def _first_run_at(data: ScheduleData) -> datetime | None:
    """The first firing, in UTC, or None when the definition cannot fire."""
    from app.services.timezones import build_trigger, next_firing_after

    try:
        trigger = build_trigger(
            timezone=data.timezone,
            cron_expression=data.cron_expression,
            run_at=data.run_at,
        )
        return next_firing_after(trigger, after=datetime.now(UTC), timezone=data.timezone)
    except Exception as exc:
        raise ScheduleInvalid(f"The schedule's timing is not usable: {exc}") from exc


def _first_run_at_from(schedule: Schedule) -> datetime | None:
    return _first_run_at(
        ScheduleData(
            name=schedule.name,
            workflow_id=schedule.workflow_id,
            schedule_kind=ScheduleKind(schedule.schedule_kind),
            timezone=schedule.timezone,
            cron_expression=schedule.cron_expression,
            run_at=as_utc(schedule.run_at) if schedule.run_at else None,
        )
    )


# --------------------------------------------------------------------------- #
# Run history
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RunHistory:
    items: list[ScheduleRun]
    total: int


def run_history(
    schedule_id: int, *, principal: Principal, limit: int = 50, offset: int = 0
) -> RunHistory:
    """The firing ledger for one schedule, newest first."""
    session = get_session_factory()()
    try:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None or not _may_view(session, schedule, principal.user_id):
            raise ScheduleNotFound(schedule_id)

        total = session.scalar(
            select(func.count()).select_from(ScheduleRun).where(
                ScheduleRun.schedule_id == schedule_id
            )
        ) or 0
        rows = session.scalars(
            select(ScheduleRun)
            .where(ScheduleRun.schedule_id == schedule_id)
            .order_by(ScheduleRun.scheduled_for.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        for row in rows:
            session.expunge(row)
        return RunHistory(items=list(rows), total=int(total))
    finally:
        session.close()
