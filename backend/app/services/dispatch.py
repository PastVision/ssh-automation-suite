"""Dispatch one logical firing of a schedule into an execution.

The ordering in this module is the whole point, so it is spelled out:

1. **Ledger first.** Insert the ``schedule_runs`` row for ``(schedule_id,
   scheduled_for)`` with the transient ``DISPATCHING`` outcome. The UNIQUE
   constraint on that pair is the idempotency barrier: a duplicate or retried
   firing collides here and is discarded before any execution could exist.
2. **Fence, then build.** In the same transaction, touch the lease with a guarded
   write (``validate_and_touch``) and then construct the execution through the
   normal ``workflows.launch`` path.
3. **Savepoint around the execution insert.** The partial unique index
   ``uq_executions_one_active_per_schedule`` refuses a second non-terminal run for
   the same schedule. That refusal is *expected* when a previous run is still
   active, so it is caught by rolling back only to a SAVEPOINT -- never the outer
   transaction. Rolling the whole transaction back would also discard the ledger
   row, and the outer ``except`` would then record the firing as ``error``. A
   plain ``try/except`` here is the classic way to lose exactly the correction the
   ledger exists to preserve.
4. **Commit once**, then start the runner. The runner must not start before the
   commit: it reads the row it is about to work on.

Anything that raises before step 4 leaves *no* execution row; the caller records
the failure outcome on the ledger row it inserted, which is why a failed dispatch
still produces a ledger entry rather than silence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.db.types import as_utc
from app.models.audit_actions import AuditAction
from app.models.enums import ExecutionStatus, ExecutionTrigger
from app.models.schedule import (
    Schedule,
    ScheduleRun,
    ScheduleRunOutcome,
    ScheduleState,
)
from app.models.user import User
from app.models.workflow import Workflow
from app.services import audit, execution_state, permissions
from app.services.execution_runner import get_runner
from app.services.execution_state import discard_execution_dir, write_execution_manifest
from app.services.lease import SchedulerLease
from app.services.sessions import Principal
from app.services.workflows import (
    RESOURCE,
    ScheduleLaunchContext,
    WorkflowInvalid,
    WorkflowNotFound,
    launch,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchResult:
    """What came of one attempt. ``execution_id`` is set only on success."""

    outcome: ScheduleRunOutcome
    execution_id: int | None = None
    detail: str | None = None

    @property
    def dispatched(self) -> bool:
        return self.outcome is ScheduleRunOutcome.DISPATCHED


def dispatch(
    schedule_id: int,
    scheduled_for: datetime,
    *,
    lease: SchedulerLease,
    catch_up: bool = False,
    session: Session | None = None,
) -> DispatchResult:
    """Dispatch one firing, or record why it did nothing.

    ``session`` exists for tests that need to assert transaction behaviour; the
    scheduler calls this without one. ``catch_up`` marks a firing recovered after
    downtime, so it can be skipped when the policy is ``skip``.
    """
    scheduled_for = as_utc(scheduled_for)
    owns_session = session is None
    session = session or get_session_factory()()

    execution_id: int | None = None
    try:
        # --- 1. ledger first: the idempotency barrier ----------------------- #
        ledger = ScheduleRun(
            schedule_id=schedule_id,
            scheduled_for=scheduled_for,
            outcome=ScheduleRunOutcome.DISPATCHING,
        )
        session.add(ledger)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return DispatchResult(
                outcome=ScheduleRunOutcome.DUPLICATE,
                detail=(
                    "This firing time was already handled; a duplicate dispatch "
                    "was discarded."
                ),
            )

        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            return _finish(
                session, ledger, ScheduleRunOutcome.ERROR, "The schedule no longer exists."
            )

        if schedule.deleted_at is not None:
            return _finish(
                session, ledger, ScheduleRunOutcome.SKIPPED_DISABLED,
                "The schedule was deleted.",
            )

        if not schedule.enabled or ScheduleState(schedule.state) in {
            ScheduleState.PAUSED,
            ScheduleState.DISABLED,
        }:
            return _finish(
                session, ledger, ScheduleRunOutcome.SKIPPED_DISABLED,
                "The schedule is paused or disabled.",
            )

        if catch_up and schedule.misfire_policy == "skip":
            return _finish(
                session, ledger, ScheduleRunOutcome.SKIPPED_MISFIRE,
                "This firing was missed while the service was down and the "
                "schedule's misfire policy is to skip it.",
            )

        # --- 2. fence: a guarded write in this transaction ------------------ #
        if not lease.validate_and_touch(session):
            return _finish(
                session, ledger, ScheduleRunOutcome.SKIPPED_FENCED,
                "Another process took over scheduling before this run started.",
            )

        actor = _resolve_principal(session, schedule)
        if actor is None:
            return _finish(
                session, ledger, ScheduleRunOutcome.SKIPPED_OWNER_INACTIVE,
                "The schedule's owner is no longer active.",
            )

        authorized = _authorize(session, schedule, actor)
        if authorized is not None:
            return _finish(session, ledger, *authorized)

        # --- 3. build inside a savepoint so an overlap is recoverable ------- #
        context = ScheduleLaunchContext(
            schedule_id=schedule.id,
            schedule_name=schedule.name,
            scheduled_for=scheduled_for,
        )
        try:
            with session.begin_nested():
                execution_id = launch(
                    schedule.workflow_id,
                    principal=actor,
                    trigger=ExecutionTrigger.SCHEDULED,
                    schedule=context,
                    session=session,
                    start_runner=False,
                )
        except IntegrityError:
            # The overlap gate, not an error: a previous run of this schedule is
            # still pending or running. Only the savepoint was rolled back, so the
            # ledger row survives to record the decision.
            logger.info(
                "Schedule %s firing %s skipped: a run is still active",
                schedule_id,
                scheduled_for,
                exc_info=True,
            )
            return _finish(
                session, ledger, ScheduleRunOutcome.SKIPPED_OVERLAP,
                "The previous run of this schedule has not finished yet.",
            )

        ledger.outcome = ScheduleRunOutcome.DISPATCHED
        ledger.execution_id = execution_id
        # The schedule's own cursor is *not* advanced here: ``schedules.record_firing``
        # is the single writer of ``last_fired_for``/``next_run_at``. Advancing in two
        # places would double-increment ``row_version`` and let the two disagree about
        # which firing comes next.
        session.commit()
    except WorkflowNotFound:
        # ``launch`` raises these before an execution row exists. The ledger row is
        # still pending in the outer transaction (any savepoint has already been
        # rolled back), so record the decision on it rather than rolling back: a
        # firing that did nothing must still say why.
        return _finish(
            session,
            ledger,
            ScheduleRunOutcome.INVALID_WORKFLOW,
            "The schedule's workflow no longer exists.",
        )
    except WorkflowInvalid as exc:
        return _finish(
            session, ledger, ScheduleRunOutcome.INVALID_WORKFLOW, str(exc)
        )
    except permissions.NotAuthorized as exc:
        # ``launch`` re-checks run permission itself; reaching here means the
        # decision changed between ``_authorize`` and the savepoint. Recorded as
        # unauthorized rather than an error, because the schedule is intact and the
        # next firing may succeed once permissions are restored.
        return _finish(
            session, ledger, ScheduleRunOutcome.UNAUTHORIZED, str(exc)
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never re-raised into APScheduler
        session.rollback()
        logger.exception("Scheduled dispatch failed for schedule %s", schedule_id)
        _record_failure(schedule_id, scheduled_for, exc)
        return DispatchResult(outcome=ScheduleRunOutcome.ERROR, detail=str(exc))
    finally:
        if owns_session:
            session.close()

    # --- 4. post-commit side effects --------------------------------------- #
    write_execution_manifest(execution_id)
    try:
        get_runner().start(execution_id)
    except RuntimeError as exc:
        # The row exists but nothing will run it: mark it failed so history is
        # truthful instead of leaving a permanently "pending" execution.
        execution_state.finish_execution(
            execution_id, status=ExecutionStatus.FAILED, error_message=str(exc)
        )
        discard_execution_dir(execution_id)
        return DispatchResult(
            outcome=ScheduleRunOutcome.ERROR,
            execution_id=execution_id,
            detail=f"The execution was created but could not start: {exc}",
        )

    audit.record(
        AuditAction.SCHEDULE_FIRED,
        actor_id=None,
        actor_username="system:scheduler",
        resource_type=RESOURCE.value,
        resource_id=schedule.workflow_id,
        detail={
            "schedule_id": schedule_id,
            "execution_id": execution_id,
            "scheduled_for": scheduled_for,
        },
    )
    return DispatchResult(
        outcome=ScheduleRunOutcome.DISPATCHED, execution_id=execution_id
    )


def _finish(
    session: Session, ledger: ScheduleRun, outcome: ScheduleRunOutcome, detail: str
) -> DispatchResult:
    """Close a ledger row with a non-dispatch outcome and commit it."""
    ledger.outcome = outcome
    ledger.detail = detail
    session.commit()
    return DispatchResult(outcome=outcome, detail=detail)


def _resolve_principal(session: Session, schedule: Schedule) -> Principal | None:
    """A principal for the schedule's owner, or None when the owner is gone.

    The owner is the execution's owner -- they see and can cancel it -- but the
    audit trail records the service actor instead, so a scheduled run is never
    attributed to a person who did nothing.
    """
    user = session.get(User, schedule.owner_id)
    if user is None or not user.is_active:
        return None
    return Principal(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        source="scheduler",
    )


def _authorize(
    session: Session, schedule: Schedule, actor: Principal
) -> tuple[ScheduleRunOutcome, str] | None:
    """Re-check access at fire time, because grants can change after creation."""
    workflow = session.get(Workflow, schedule.workflow_id)
    if workflow is None:
        return (
            ScheduleRunOutcome.INVALID_WORKFLOW,
            "The schedule's workflow no longer exists.",
        )
    if not permissions.can_view(RESOURCE, workflow.id, workflow.owner_id, actor.user_id):
        return (
            ScheduleRunOutcome.UNAUTHORIZED,
            "The schedule's owner can no longer access this workflow.",
        )
    # A schedule runs with its owner's rights, so it is bound by the same
    # ``execution_permission`` rule as a manual run. Ownership or sharing can change
    # after the schedule was created, which would otherwise leave a schedule firing
    # SSH work its owner is no longer allowed to start. ``launch`` enforces this too;
    # checking here records the decision on the ledger instead of surfacing as an
    # error after a savepoint rollback.
    if not permissions.can_run_workflow(
        workflow_id=workflow.id,
        owner_id=workflow.owner_id,
        user_id=actor.user_id,
        execution_permission=workflow.execution_permission,
    ):
        return (
            ScheduleRunOutcome.UNAUTHORIZED,
            "The schedule's owner is no longer permitted to run this workflow.",
        )
    return None


def record_skipped(schedule_id: int, firings: list[datetime], *, detail: str) -> None:
    """Record firings the scheduler decided not to dispatch.

    Used when the misfire policy collapses a burst: the skipped instants still get
    a ledger row, so "why did my workflow not run at 03:00?" has an answer rather
    than a hole. Duplicates are ignored, which makes this safe to call again after
    a crash.
    """
    if not firings:
        return
    session = get_session_factory()()
    try:
        for scheduled_for in firings:
            session.add(
                ScheduleRun(
                    schedule_id=schedule_id,
                    scheduled_for=as_utc(scheduled_for),
                    outcome=ScheduleRunOutcome.SKIPPED_MISFIRE,
                    detail=detail,
                )
            )
        try:
            session.commit()
        except IntegrityError:
            # A concurrent or replayed call already recorded one of them.
            session.rollback()
    except Exception:  # pragma: no cover - the decision still stands
        session.rollback()
        logger.exception("Could not record skipped firings for schedule %s", schedule_id)
    finally:
        session.close()


def _record_failure(schedule_id: int, scheduled_for: datetime, exc: Exception) -> None:
    """Best-effort ledger entry for an unexpected failure after a rollback."""
    session = get_session_factory()()
    try:
        existing = session.scalars(
            select(ScheduleRun).where(
                ScheduleRun.schedule_id == schedule_id,
                ScheduleRun.scheduled_for == scheduled_for,
            )
        ).first()
        if existing is None:
            session.add(
                ScheduleRun(
                    schedule_id=schedule_id,
                    scheduled_for=scheduled_for,
                    outcome=ScheduleRunOutcome.ERROR,
                    detail=str(exc)[:2000],
                )
            )
        else:
            existing.outcome = ScheduleRunOutcome.ERROR
            existing.detail = str(exc)[:2000]
        session.commit()
    except Exception:  # pragma: no cover - never mask the original failure
        session.rollback()
        logger.exception("Could not record a failed scheduled dispatch")
    finally:
        session.close()
