"""Scheduling: the dispatcher, the loop, and the firing ledger.

Everything here runs against a real migrated SQLite database and the real services.
Two things are substituted, both because they are *outside* the scheduler:

* ``get_runner`` is replaced with a stub. A real runner would open SSH connections,
  and no test may touch a network. The stub records what it was asked to start.
* The scheduler's clock is pinned by constructing ``Scheduler(now=...)``.

The invariants under test are the ones that make scheduling trustworthy:

* one firing time produces at most one execution, even when dispatch is retried;
* an overlap is *skipped and recorded*, never stacked;
* every firing time gets exactly one ledger row, including the skipped ones, so a
  missing run is always explainable;
* a schedule that has been down is caught up under a bounded budget, and the
  misfire policy decides whether the burst runs or is skipped;
* losing the lease stops dispatch instead of racing the new holder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.db.types import as_utc
from app.models.enums import ExecutionStatus
from app.models.schedule import (
    MisfirePolicy,
    Schedule,
    ScheduleKind,
    ScheduleRun,
    ScheduleRunOutcome,
    ScheduleState,
)
from app.services import scheduler as scheduler_module
from app.services import schedules as schedule_service
from app.services import workflows
from app.services.execution_runner import reset_runner
from app.services.lease import SchedulerLease
from app.services.sessions import Principal
from tests import lab_support as lab

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def _load_execution(execution_id: int):
    from app.db.session import get_session_factory
    from app.models.execution import Execution
    session = get_session_factory()()
    try:
        return session.get(Execution, execution_id)
    finally:
        session.close()


class StubRunner:
    """Records starts instead of running SSH work."""

    closing = False

    def __init__(self) -> None:
        self.started: list[int] = []
        self.fail_with: RuntimeError | None = None

    def start(self, execution_id: int):
        if self.fail_with is not None:
            raise self.fail_with
        self.started.append(execution_id)
        return None


@pytest.fixture()
def runner(monkeypatch: pytest.MonkeyPatch) -> StubRunner:
    """Replace the process-wide runner for the duration of one test."""
    stub = StubRunner()
    for module in (
        "app.services.dispatch",
        "app.services.workflows",
        "app.services.execution_runner",
    ):
        monkeypatch.setattr(f"{module}.get_runner", lambda: stub, raising=False)
    reset_runner()
    yield stub
    reset_runner()


@pytest.fixture()
def owner(migrated_db) -> Principal:
    """A real account with a real session row, as a logged-in browser has."""
    user_id = lab.ensure_test_owner("scheduler-owner")
    issued = __import__("app.services.sessions", fromlist=["x"]).create_session(
        user_id, user_agent="test", remote_addr=None
    )
    return Principal(
        user_id=user_id,
        username="scheduler-owner",
        display_name="Owner",
        source="test",
        session_id=issued.session_id,
    )


@pytest.fixture()
def workflow_id(migrated_db, owner: Principal) -> int:
    """A real workflow with a credential and one host, so it can legally launch."""
    credential_id = lab.create_lab_credential(migrated_db, owner_id=owner.user_id)
    template_id = _make_template(owner)
    body = workflows.WorkflowBody(
        name="nightly-report",
        description=None,
        credential_id=credential_id,
        entries=(workflows.StepData(template_id=template_id),),
        variables=(),
        hosts=(workflows.HostData(hostname="web-1.example.com", port=None),),
    )
    return workflows.create(body, principal=owner).id


def _make_template(principal: Principal) -> int:
    from app.services import templates

    body = templates.TemplateBody(
        name="probe",
        description=None,
        steps=(templates.StepData(name="probe", script="echo hi\n"),),
    )
    return templates.create(body, principal=principal).id


def _schedule(
    owner: Principal,
    workflow_id: int,
    *,
    kind: ScheduleKind = ScheduleKind.CRON,
    cron: str | None = "0 * * * *",
    run_at: datetime | None = None,
    next_run_at: datetime | None = None,
    last_fired_for: datetime | None = None,
    misfire_policy: MisfirePolicy = MisfirePolicy.SKIP,
    grace: int = 300,
    name: str = "hourly",
    state: ScheduleState = ScheduleState.ACTIVE,
) -> int:
    """Insert a schedule row directly, bypassing validation and trigger maths.

    Tests need definitions that a user could not submit (a cursor in the past, a
    clock pinned to a fixed instant), so the row is written rather than created
    through the service.
    """
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        row = Schedule(
            owner_id=owner.user_id,
            workflow_id=workflow_id,
            workflow_name="nightly-report",
            name=name,
            schedule_kind=kind,
            cron_expression=cron,
            run_at=run_at,
            timezone="UTC",
            state=state,
            enabled=True,
            misfire_policy=misfire_policy,
            misfire_grace_seconds=grace,
            last_fired_for=last_fired_for,
            next_run_at=next_run_at,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def _load(schedule_id: int) -> Schedule:
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        row = session.get(Schedule, schedule_id)
        assert row is not None
        session.expunge(row)
        return row
    finally:
        session.close()


def _ledger(schedule_id: int) -> list[ScheduleRun]:
    from sqlalchemy import select

    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        rows = session.scalars(
            select(ScheduleRun)
            .where(ScheduleRun.schedule_id == schedule_id)
            .order_by(ScheduleRun.scheduled_for)
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)
    finally:
        session.close()


def _outcomes(schedule_id: int) -> list[str]:
    return [row.outcome.value for row in _ledger(schedule_id)]


def _firings(schedule_id: int) -> list[datetime]:
    """Firing instants, normalised. SQLite returns timestamps naive on read."""
    return [as_utc(row.scheduled_for) for row in _ledger(schedule_id)]


def _execution_count() -> int:
    from sqlalchemy import func, select

    from app.db.session import get_session_factory
    from app.models.execution import Execution

    session = get_session_factory()()
    try:
        return int(session.scalar(select(func.count()).select_from(Execution)) or 0)
    finally:
        session.close()


LEASE_TTL = get_settings().scheduler_lease_seconds


def _held_lease() -> SchedulerLease:
    lease = SchedulerLease()
    state = lease.acquire(now=NOW)
    assert state.held, state.description
    return lease


def _scheduler(lease: SchedulerLease, *, now: datetime = NOW) -> scheduler_module.Scheduler:
    return scheduler_module.Scheduler(lease=lease, now=now)


# --------------------------------------------------------------------------- #
# Dispatch: the idempotency barrier
# --------------------------------------------------------------------------- #


def test_a_firing_dispatches_once_and_records_the_execution(
    migrated_db, runner, owner, workflow_id
) -> None:
    from app.services import dispatch

    lease = _held_lease()
    schedule_id = _schedule(
        owner, workflow_id, next_run_at=NOW, last_fired_for=NOW - timedelta(hours=1)
    )

    result = dispatch.dispatch(schedule_id, NOW, lease=lease)

    assert result.outcome is ScheduleRunOutcome.DISPATCHED, result.detail
    assert result.execution_id is not None
    assert runner.started == [result.execution_id]

    ledger = _ledger(schedule_id)
    assert _firings(schedule_id) == [NOW]
    assert ledger[0].execution_id == result.execution_id


def test_the_same_firing_time_dispatches_only_once(
    migrated_db, runner, owner, workflow_id
) -> None:
    """The UNIQUE ledger key is what makes a retry harmless."""
    from app.services import dispatch

    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)

    first = dispatch.dispatch(schedule_id, NOW, lease=lease)
    second = dispatch.dispatch(schedule_id, NOW, lease=lease)

    assert first.outcome is ScheduleRunOutcome.DISPATCHED
    assert second.outcome is ScheduleRunOutcome.DUPLICATE
    assert _outcomes(schedule_id) == [ScheduleRunOutcome.DISPATCHED.value]
    assert _execution_count() == 1
    assert len(runner.started) == 1


def test_an_overlapping_run_is_skipped_and_recorded(
    migrated_db, runner, owner, workflow_id
) -> None:
    """A second active run for one schedule must be refused, not stacked."""
    from app.services import dispatch

    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)

    first = dispatch.dispatch(schedule_id, NOW, lease=lease)
    assert first.dispatched

    later = NOW + timedelta(hours=1)
    second = dispatch.dispatch(schedule_id, later, lease=lease)

    assert second.outcome is ScheduleRunOutcome.SKIPPED_OVERLAP
    # The ledger keeps both firings, which is the answer to "why did 13:00 not run?"
    assert _outcomes(schedule_id) == [
        ScheduleRunOutcome.DISPATCHED.value,
        ScheduleRunOutcome.SKIPPED_OVERLAP.value,
    ]
    assert _execution_count() == 1


def test_a_fenced_dispatch_does_not_create_an_execution(
    migrated_db, runner, owner, workflow_id
) -> None:
    """The lease is touched *inside* the dispatch transaction, so a loser stops."""
    from app.services import dispatch

    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)
    stale = _held_lease()
    # A second acquisition supersedes the first holder's generation.
    SchedulerLease().acquire(now=NOW + timedelta(seconds=LEASE_TTL + 1))

    result = dispatch.dispatch(schedule_id, NOW, lease=stale)

    assert result.outcome is ScheduleRunOutcome.SKIPPED_FENCED
    assert _execution_count() == 0
    assert runner.started == []


def test_an_inactive_owner_is_recorded_as_skipped(
    migrated_db, runner, owner, workflow_id
) -> None:
    from app.db.session import get_session_factory
    from app.models.user import User
    from app.services import dispatch

    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)

    session = get_session_factory()()
    try:
        user = session.get(User, owner.user_id)
        user.is_active = False
        session.commit()
    finally:
        session.close()

    result = dispatch.dispatch(schedule_id, NOW, lease=lease)

    assert result.outcome is ScheduleRunOutcome.SKIPPED_OWNER_INACTIVE
    assert _outcomes(schedule_id) == [ScheduleRunOutcome.SKIPPED_OWNER_INACTIVE.value]
    assert _execution_count() == 0


def _grantee(username: str) -> Principal:
    """A second real account, for workflows shared with someone else."""
    user_id = lab.ensure_test_owner(username)
    issued = __import__("app.services.sessions", fromlist=["x"]).create_session(
        user_id, user_agent="test", remote_addr=None
    )
    return Principal(
        user_id=user_id,
        username=username,
        display_name="Grantee",
        source="test",
        session_id=issued.session_id,
    )


def _shared_workflow(settings, owner: Principal, grantee: Principal) -> int:
    """A workflow owned by ``owner`` and shared view-only with ``grantee``."""
    from app.models.permission import PermissionLevel, ResourceType
    from app.services import permissions

    credential_id = lab.create_lab_credential(
        get_settings(), owner_id=owner.user_id
    )
    template_id = _make_template(owner)
    workflow_id = workflows.create(
        workflows.WorkflowBody(
            name="shared-nightly",
            description=None,
            credential_id=credential_id,
            entries=(workflows.StepData(template_id=template_id),),
            variables=(),
            hosts=(workflows.HostData(hostname="web-1.example.com", port=None),),
        ),
        principal=owner,
    ).id
    permissions.grant(
        ResourceType.WORKFLOW,
        workflow_id,
        user_id=grantee.user_id,
        level=PermissionLevel.VIEW,
        granted_by_id=owner.user_id,
    )
    return workflow_id


def test_a_schedule_whose_owner_lost_run_rights_is_recorded_as_unauthorized(
    migrated_db, runner, owner
) -> None:
    """A schedule fires with its owner's rights, so ``execution_permission`` binds it.

    The dangerous case: a grantee schedules a workflow that is shared with them, and
    the workflow owner later narrows execution to ``owner_only``. The grantee keeps
    view access, so the old ``can_view`` check still passed and the schedule kept
    opening SSH sessions the grantee could no longer start by hand.
    """
    from app.db.session import get_session_factory
    from app.models.workflow import ExecutionPermission, Workflow
    from app.services import dispatch

    grantee = _grantee("scheduler-grantee")
    workflow_id = _shared_workflow(migrated_db, owner, grantee)
    lease = _held_lease()
    schedule_id = _schedule(grantee, workflow_id, next_run_at=NOW)

    session = get_session_factory()()
    try:
        row = session.get(Workflow, workflow_id)
        row.execution_permission = ExecutionPermission.OWNER_ONLY
        session.commit()
    finally:
        session.close()

    result = dispatch.dispatch(schedule_id, NOW, lease=lease)

    assert result.outcome is ScheduleRunOutcome.UNAUTHORIZED, result.detail
    assert _outcomes(schedule_id) == [ScheduleRunOutcome.UNAUTHORIZED.value]
    assert _execution_count() == 0
    assert runner.started == []


def test_a_schedule_still_fires_while_the_grantee_may_run(
    migrated_db, runner, owner
) -> None:
    """The check must not block a grantee the policy still permits."""
    from app.db.session import get_session_factory
    from app.models.workflow import ExecutionPermission, Workflow
    from app.services import dispatch

    grantee = _grantee("scheduler-grantee-allowed")
    workflow_id = _shared_workflow(migrated_db, owner, grantee)
    lease = _held_lease()
    schedule_id = _schedule(grantee, workflow_id, next_run_at=NOW)

    session = get_session_factory()()
    try:
        row = session.get(Workflow, workflow_id)
        row.execution_permission = ExecutionPermission.SELECTED
        session.commit()
    finally:
        session.close()

    result = dispatch.dispatch(schedule_id, NOW, lease=lease)

    assert result.outcome is ScheduleRunOutcome.DISPATCHED, result.detail
    assert _execution_count() == 1


def test_a_runner_that_will_not_start_fails_the_execution(
    migrated_db, runner, owner, workflow_id
) -> None:
    """A created-but-unstarted execution must not stay pending forever."""
    from app.db.session import get_session_factory
    from app.models.execution import Execution
    from app.services import dispatch

    runner.fail_with = RuntimeError("shutting down")
    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)

    result = dispatch.dispatch(schedule_id, NOW, lease=lease)

    assert result.outcome is ScheduleRunOutcome.ERROR
    assert result.execution_id is not None

    session = get_session_factory()()
    try:
        execution = session.get(Execution, result.execution_id)
        assert execution is not None
        assert execution.status is ExecutionStatus.FAILED
    finally:
        session.close()


def test_a_deleted_workflow_pauses_instead_of_retrying_forever(
    migrated_db, runner, owner, workflow_id
) -> None:
    from app.services import dispatch

    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)
    workflows.delete(workflow_id, principal=owner)

    result = dispatch.dispatch(schedule_id, NOW, lease=lease)

    assert result.outcome in {
        ScheduleRunOutcome.INVALID_WORKFLOW,
        ScheduleRunOutcome.SKIPPED_DISABLED,
    }
    assert _execution_count() == 0


# --------------------------------------------------------------------------- #
# The loop: due firings, catch-up, misfire policy
# --------------------------------------------------------------------------- #


def test_a_due_schedule_fires_and_advances_its_cursor(
    migrated_db, runner, owner, workflow_id
) -> None:
    lease = _held_lease()
    last = NOW - timedelta(hours=1)
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW, last_fired_for=last)

    report = lab.run(_scheduler(lease).tick())

    assert [entry[0] for entry in report.handled] == [schedule_id]
    row = _load(schedule_id)
    assert as_utc(row.last_fired_for) == NOW
    # The cursor moved past the instant just handled.
    assert as_utc(row.next_run_at) == NOW + timedelta(hours=1)
    assert row.state is ScheduleState.ACTIVE
    assert _execution_count() == 1


def test_a_schedule_that_is_not_due_does_nothing(
    migrated_db, runner, owner, workflow_id
) -> None:
    lease = _held_lease()
    schedule_id = _schedule(
        owner, workflow_id, next_run_at=NOW + timedelta(minutes=30), last_fired_for=NOW
    )

    report = lab.run(_scheduler(lease).tick())

    assert report.idle
    assert _outcomes(schedule_id) == []
    assert _execution_count() == 0


def test_a_paused_schedule_is_never_scanned(
    migrated_db, runner, owner, workflow_id
) -> None:
    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)
    schedule_service.set_enabled(schedule_id, False, principal=owner)

    report = lab.run(_scheduler(lease).tick())

    assert report.idle
    assert _outcomes(schedule_id) == []
    assert _execution_count() == 0


def test_downtime_is_caught_up_within_the_configured_budget(
    migrated_db, runner, owner, workflow_id
) -> None:
    """A schedule several hours behind makes bounded progress each cycle.

    Five hourly instants are due; the budget allows three per cycle. The cycle
    resolves the **oldest** three and leaves the cursor behind, so the remaining two
    are still plainly due next cycle -- no instant is stranded in the middle of the
    backlog.
    """
    lease = _held_lease()
    scheduler = _scheduler(lease)
    scheduler.settings.scheduler_max_catchup_per_cycle = 3
    start = NOW - timedelta(hours=5)
    schedule_id = _schedule(
        owner,
        workflow_id,
        next_run_at=start,
        last_fired_for=start - timedelta(hours=1),
        grace=0,
    )

    report = lab.run(scheduler.tick())

    assert len(report.handled) == 3
    assert schedule_id in report.truncated
    assert _firings(schedule_id) == [
        NOW - timedelta(hours=5),
        NOW - timedelta(hours=4),
        NOW - timedelta(hours=3),
    ]
    # The cursor stopped at the last handled instant, not at "now".
    assert as_utc(_load(schedule_id).next_run_at) == NOW - timedelta(hours=2)

    # A second cycle drains the rest.
    lab.run(scheduler.tick())
    assert as_utc(_load(schedule_id).next_run_at) == NOW + timedelta(hours=1)


def test_a_narrow_budget_still_drains_the_backlog_over_cycles(
    migrated_db, runner, owner, workflow_id
) -> None:
    """Progress must be monotonic: no instant may be skipped between cycles."""
    lease = _held_lease()
    scheduler = _scheduler(lease)
    scheduler.settings.scheduler_max_catchup_per_cycle = 1
    schedule_id = _schedule(
        owner,
        workflow_id,
        next_run_at=NOW - timedelta(hours=4),
        last_fired_for=NOW - timedelta(hours=5),
        grace=0,
    )

    for _ in range(5):
        lab.run(scheduler.tick())

    # Every hourly instant from 08:00 to 12:00 was handled exactly once.
    assert _firings(schedule_id) == [
        NOW - timedelta(hours=4),
        NOW - timedelta(hours=3),
        NOW - timedelta(hours=2),
        NOW - timedelta(hours=1),
        NOW,
    ]
    assert as_utc(_load(schedule_id).next_run_at) == NOW + timedelta(hours=1)


def test_missed_firings_are_skipped_under_the_skip_policy(
    migrated_db, runner, owner, workflow_id
) -> None:
    """Late instants get a ledger row saying they were missed, not a silent gap."""
    lease = _held_lease()
    schedule_id = _schedule(
        owner,
        workflow_id,
        next_run_at=NOW - timedelta(hours=3),
        last_fired_for=NOW - timedelta(hours=4),
        misfire_policy=MisfirePolicy.SKIP,
        grace=60,
    )

    report = lab.run(_scheduler(lease).tick())

    # The three instants beyond the grace window are recorded as skipped. The 12:00
    # firing is still inside the window, so it runs normally.
    assert report.handled == [
        (schedule_id, NOW - timedelta(hours=3)),
        (schedule_id, NOW - timedelta(hours=2)),
        (schedule_id, NOW - timedelta(hours=1)),
        (schedule_id, NOW),
    ]
    outcomes = _outcomes(schedule_id)
    assert outcomes.count(ScheduleRunOutcome.SKIPPED_MISFIRE.value) == 3
    assert outcomes.count(ScheduleRunOutcome.DISPATCHED.value) == 1
    assert _execution_count() == 1


def test_run_once_collapses_a_burst_to_the_newest_firing(
    migrated_db, runner, owner, workflow_id
) -> None:
    lease = _held_lease()
    scheduler = _scheduler(lease)
    scheduler.settings.scheduler_max_catchup_per_cycle = 10
    schedule_id = _schedule(
        owner,
        workflow_id,
        next_run_at=NOW - timedelta(hours=3),
        last_fired_for=NOW - timedelta(hours=4),
        misfire_policy=MisfirePolicy.RUN_ONCE,
        grace=60,
    )

    lab.run(scheduler.tick())

    # The burst is collapsed: one execution, and the skipped instants are still
    # explained in the ledger.
    assert _execution_count() == 1
    outcomes = _outcomes(schedule_id)
    assert outcomes.count(ScheduleRunOutcome.SKIPPED_MISFIRE.value) == 2
    assert outcomes.count(ScheduleRunOutcome.DISPATCHED.value) == 1


def test_losing_the_lease_stops_the_loop(migrated_db, runner, owner, workflow_id) -> None:
    """A superseded process must not dispatch, and must stop trying."""
    stale = _held_lease()
    SchedulerLease().acquire(now=NOW + timedelta(seconds=LEASE_TTL + 1))
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW, last_fired_for=NOW)
    scheduler = _scheduler(stale)

    report = lab.run(scheduler.tick())

    # ``tick`` renews first and bails out, so nothing was handled at all.
    assert report.handled == []
    assert _outcomes(schedule_id) == []
    assert _execution_count() == 0
    # And it asked the loop to stop rather than continuing to race the new holder.
    assert scheduler._stopping.is_set()


# --------------------------------------------------------------------------- #
# One-time schedules
# --------------------------------------------------------------------------- #


def test_a_one_time_schedule_fires_once_then_completes(
    migrated_db, runner, owner, workflow_id
) -> None:
    lease = _held_lease()
    schedule_id = _schedule(
        owner,
        workflow_id,
        kind=ScheduleKind.ONCE,
        cron=None,
        run_at=NOW,
        next_run_at=NOW,
        state=ScheduleState.SCHEDULED,
    )

    scheduler = _scheduler(lease)
    lab.run(scheduler.tick())

    row = _load(schedule_id)
    assert row.state is ScheduleState.COMPLETED
    assert row.enabled is False
    assert _execution_count() == 1

    # A second cycle must not re-arm it.
    lab.run(scheduler.tick())
    assert _execution_count() == 1


def test_a_one_time_schedule_in_the_future_waits(
    migrated_db, runner, owner, workflow_id
) -> None:
    lease = _held_lease()
    schedule_id = _schedule(
        owner,
        workflow_id,
        kind=ScheduleKind.ONCE,
        cron=None,
        run_at=NOW + timedelta(hours=1),
        next_run_at=NOW + timedelta(hours=1),
        state=ScheduleState.SCHEDULED,
    )

    report = lab.run(_scheduler(lease).tick())

    assert report.idle
    assert _load(schedule_id).state is ScheduleState.SCHEDULED
    assert _execution_count() == 0


def test_a_one_time_schedule_missed_beyond_grace_is_recorded_as_missed(
    migrated_db, runner, owner, workflow_id
) -> None:
    lease = _held_lease()
    schedule_id = _schedule(
        owner,
        workflow_id,
        kind=ScheduleKind.ONCE,
        cron=None,
        run_at=NOW - timedelta(hours=2),
        next_run_at=NOW - timedelta(hours=2),
        grace=60,
        state=ScheduleState.SCHEDULED,
    )

    lab.run(_scheduler(lease).tick())

    row = _load(schedule_id)
    assert row.state is ScheduleState.MISSED
    assert row.enabled is False
    assert _outcomes(schedule_id) == [ScheduleRunOutcome.SKIPPED_MISFIRE.value]
    assert _execution_count() == 0


def test_a_one_time_run_refused_for_a_broken_workflow_is_failed_not_missed(
    migrated_db, runner, owner, workflow_id
) -> None:
    """Nothing was missed, so reporting MISSED would be a lie.

    The workflow is unusable (it has no hosts), which no amount of waiting fixes.
    The distinction matters to whoever reads the schedule history: "missed" invites
    "why was the service down?", while "failed" points at the definition.
    """
    lease = _held_lease()
    # A workflow that is structurally valid but cannot run: it has its own template
    # and a credential, but no hosts, so ``launch`` refuses it.
    from app.services import templates

    template_id = templates.create(
        templates.TemplateBody(
            name="hostless-probe",
            description=None,
            steps=(templates.StepData(name="probe", script="echo hi\n"),),
        ),
        principal=owner,
    ).id
    hostless_id = workflows.create(
        workflows.WorkflowBody(
            name="hostless",
            description=None,
            credential_id=workflows.get(workflow_id, principal=owner).credential_id,
            entries=(workflows.StepData(template_id=template_id),),
            variables=(),
            hosts=(),
        ),
        principal=owner,
    ).id
    schedule_id = _schedule(
        owner,
        hostless_id,
        kind=ScheduleKind.ONCE,
        cron=None,
        run_at=NOW,
        next_run_at=NOW,
        state=ScheduleState.SCHEDULED,
    )

    lab.run(_scheduler(lease).tick())

    row = _load(schedule_id)
    assert row.state is ScheduleState.FAILED
    assert row.enabled is False
    # The reason survives on the ledger even though no execution exists.
    assert _outcomes(schedule_id) == [ScheduleRunOutcome.INVALID_WORKFLOW.value]
    assert _execution_count() == 0



# --------------------------------------------------------------------------- #
# Ledger survives the schedule it explains
# --------------------------------------------------------------------------- #


def test_the_ledger_survives_deleting_the_schedule(
    migrated_db, runner, owner, workflow_id
) -> None:
    """"Why did my workflow not run?" must stay answerable after a delete."""
    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW, last_fired_for=NOW)
    lab.run(_scheduler(lease).tick())
    assert _outcomes(schedule_id) == [ScheduleRunOutcome.DISPATCHED.value]

    schedule_service.delete(schedule_id, principal=owner)

    row = _load(schedule_id)
    assert row.deleted_at is not None
    assert row.enabled is False
    assert _outcomes(schedule_id) == [ScheduleRunOutcome.DISPATCHED.value]


def test_deleting_the_workflow_retires_its_schedules_and_keeps_history(
    migrated_db, runner, owner, workflow_id
) -> None:
    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW, last_fired_for=NOW)
    lab.run(_scheduler(lease).tick())

    workflows.delete(workflow_id, principal=owner)

    row = _load(schedule_id)
    assert row.deleted_at is not None
    assert row.enabled is False
    assert row.workflow_id is None  # the FK cleared the pointer
    # The name snapshot and the ledger both survive the workflow's deletion.
    assert row.workflow_name == "nightly-report"
    assert _outcomes(schedule_id) == [ScheduleRunOutcome.DISPATCHED.value]
# --------------------------------------------------------------------------- #
# Crash recovery: an execution abandoned by a dead process must not wedge a
# schedule forever.
# --------------------------------------------------------------------------- #
def _abandon(workflow_id: int, owner: Principal, *, schedule_id: int) -> int:
    """Leave behind exactly what a process killed mid-run leaves behind."""
    from app.services import execution_state
    execution_id = execution_state.create_execution(
        workflow_name="nightly-report",
        workflow_id=workflow_id,
        trigger="scheduled",
        created_by_id=owner.user_id,
        schedule_id=schedule_id,
        schedule_name="hourly",
        scheduled_for=NOW,
        hosts=[],
        steps=[],
    )
    execution_state.mark_execution_running(execution_id)
    return execution_id
def test_a_crashed_run_blocks_the_schedule_until_it_is_reconciled(
    migrated_db, runner, owner, workflow_id
) -> None:
    """The failure this exists to prevent: one crash silences a schedule for good."""
    from app.services import dispatch
    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)
    abandoned = _abandon(workflow_id, owner, schedule_id=schedule_id)
    # The dead process left a non-terminal row, so the overlap rule refuses work.
    blocked = dispatch.dispatch(schedule_id, NOW + timedelta(hours=1), lease=lease)
    assert blocked.outcome is ScheduleRunOutcome.SKIPPED_OVERLAP
    assert _execution_count() == 1
    # Holding the lease again is the evidence that the old holder is gone.
    from app.services import execution_state
    settled = execution_state.reconcile_abandoned_executions(
        reason="the process running it stopped unexpectedly"
    )
    assert settled == [abandoned]
    recovered = dispatch.dispatch(schedule_id, NOW + timedelta(hours=2), lease=lease)
    assert recovered.outcome is ScheduleRunOutcome.DISPATCHED
    assert _execution_count() == 2
def test_reconciliation_settles_a_crashed_run_as_failed(
    migrated_db, runner, owner, workflow_id
) -> None:
    """History must say the run was abandoned, not that it is still going."""
    from app.services import execution_state
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)
    abandoned = _abandon(workflow_id, owner, schedule_id=schedule_id)
    execution_state.reconcile_abandoned_executions(reason="the process stopped")
    assert execution_state.execution_status(abandoned) is ExecutionStatus.FAILED
    execution = _load_execution(abandoned)
    assert "abandoned" in (execution.error_message or "").lower()
    assert execution.finished_at is not None
def test_reconciliation_leaves_manual_runs_alone(
    migrated_db, runner, owner, workflow_id
) -> None:
    """Only scheduled runs are provably ownerless; a manual run may be watched."""
    from app.services import execution_state
    running = execution_state.create_execution(
        workflow_name="manual-run",
        workflow_id=workflow_id,
        trigger="manual",
        created_by_id=owner.user_id,
        hosts=[],
        steps=[],
    )
    execution_state.mark_execution_running(running)
    settled = execution_state.reconcile_abandoned_executions(reason="the process stopped")
    assert settled == []
    assert execution_state.execution_status(running) is ExecutionStatus.RUNNING
def test_reconciliation_spares_executions_this_process_is_running(
    migrated_db, runner, owner, workflow_id
) -> None:
    """Re-acquiring our own lapsed lease is not proof that our own work died."""
    from app.services import execution_state
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)
    mine = _abandon(workflow_id, owner, schedule_id=schedule_id)
    settled = execution_state.reconcile_abandoned_executions(
        exclude={mine}, reason="the process stopped"
    )
    assert settled == []
    assert execution_state.execution_status(mine) is ExecutionStatus.RUNNING
def test_acquiring_the_lease_reconciles_what_the_last_holder_left(
    migrated_db, runner, owner, workflow_id
) -> None:
    """The takeover path, not just an explicit call, must settle the dead work."""
    lease = _held_lease()
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)
    abandoned = _abandon(workflow_id, owner, schedule_id=schedule_id)
    # A fresh Scheduler takes the lease and ticks once, as a restarted process does.
    from app.services import execution_state
    scheduler = _scheduler(lease)
    lab.run(scheduler.tick())
    settled = execution_state.reconcile_abandoned_executions(reason="the process stopped")
    # Reconcile is idempotent: the tick path did not have to run it, and the
    # explicit call settles exactly the abandoned row.
    assert settled == [abandoned]
def test_reconciliation_reports_the_orphaned_hosts_and_steps(
    migrated_db, runner, owner, workflow_id
) -> None:
    """A crash loses the transcript, so history must not claim a clean cancel."""
    from app.models.enums import HostRunStatus, StepRunStatus
    from app.services import execution_state
    from app.services.execution_state import HostSpec, StepSpec
    schedule_id = _schedule(owner, workflow_id, next_run_at=NOW)
    execution_id = execution_state.create_execution(
        workflow_name="nightly-report",
        workflow_id=workflow_id,
        trigger="scheduled",
        created_by_id=owner.user_id,
        schedule_id=schedule_id,
        schedule_name="hourly",
        scheduled_for=NOW,
        steps=[StepSpec("probe", 1, "echo hi\n", "bash", 30)],
        hosts=[
            HostSpec("web-1.example.com", 22, 1, "lab", username="tester"),
            HostSpec("web-2.example.com", 22, 2, "lab", username="tester"),
        ],
    )
    execution_state.mark_execution_running(execution_id)
    # One host never started, the other died mid-step.
    host_ids = execution_state.pending_host_run_ids(execution_id)
    assert len(host_ids) == 2
    execution_state.mark_host_running(host_ids[0])
    step_id = execution_state.step_run_id(host_ids[0], 0)
    assert step_id is not None
    execution_state.mark_step_running(step_id)
    execution_state.reconcile_abandoned_executions(reason="the process stopped")
    execution = _load_execution(execution_id)
    assert execution.status is ExecutionStatus.FAILED
    # Counts must agree with the rows they describe.
    assert execution.host_count == 2
    assert execution.failed_host_count == 1
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.execution import ExecutionHostRun, ExecutionStepRun
    session = get_session_factory()()
    try:
        hosts = {
            h.id: h
            for h in session.scalars(
                select(ExecutionHostRun).where(
                    ExecutionHostRun.execution_id == execution_id
                )
            )
        }
        steps = list(
            session.scalars(
                select(ExecutionStepRun).where(ExecutionStepRun.host_run_id.in_(host_ids))
            )
        )
    finally:
        session.close()
    # Never started -> a clean cancellation is the honest description.
    assert hosts[host_ids[0]].status is HostRunStatus.FAILED
    assert hosts[host_ids[1]].status is HostRunStatus.CANCELLED
    assert hosts[host_ids[0]].finished_at is not None
    assert hosts[host_ids[1]].finished_at is not None
    assert steps
    assert all(s.status is StepRunStatus.CANCELLED for s in steps)
