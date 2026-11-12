"""A scheduled firing that really runs over SSH in the Docker lab.

The scheduling suite proves the decision logic with a stub runner: the ledger,
the fenced lease, the overlap index. What it cannot show is that the execution
a firing creates is one the real runner can actually carry out -- that the
snapshot taken at dispatch time is complete enough to execute, that the run is
attributed to the schedule, and that the transcript lands where the history
says it does.

Everything here opens a real SSH connection to the Docker test lab.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.types import as_utc
from app.models.enums import ExecutionStatus, ExecutionTrigger, HostRunStatus
from app.models.schedule import MisfirePolicy, ScheduleKind, ScheduleRunOutcome
from app.services import dispatch, execution_state, schedules, templates, workflows
from app.services.execution_runner import CancelToken, ExecutionRunner, reset_runner
from app.services.lease import SchedulerLease
from app.services.sessions import Principal, create_session
from tests import lab_support as lab

pytestmark = pytest.mark.lab


@pytest.fixture()
def lab_env(settings, migrated_db, monkeypatch):
    """Point the SSH stack at the lab and return a usable credential."""
    resolved = lab.configure_lab_settings(monkeypatch)
    credential_id = lab.create_lab_credential(resolved)
    reset_runner()
    yield resolved, credential_id
    reset_runner()


@pytest.fixture()
def parked_runner(monkeypatch):
    """Accept the work without running it, so a row can be left non-terminal."""

    class Parked:
        closing = False
        started: list[int] = []

        def start(self, execution_id: int):
            self.started.append(execution_id)
            return None

    stub = Parked()
    for module in (
        "app.services.dispatch",
        "app.services.workflows",
        "app.services.execution_runner",
    ):
        monkeypatch.setattr(f"{module}.get_runner", lambda: stub, raising=False)
    reset_runner()
    yield stub
    reset_runner()


def _owner(username: str) -> Principal:
    user_id = lab.ensure_test_owner(username)
    issued = create_session(user_id, user_agent="test", remote_addr=None)
    return Principal(
        user_id=user_id,
        username=username,
        display_name="Owner",
        source="test",
        session_id=issued.session_id,
    )


def _workflow(owner: Principal, credential_id: int, *, marker: str) -> int:
    template_id = templates.create(
        templates.TemplateBody(
            name="probe",
            description=None,
            steps=(templates.StepData(name="probe", script=f"echo {marker}\n"),),
        ),
        principal=owner,
    ).id
    return workflows.create(
        workflows.WorkflowBody(
            name="scheduled-lab",
            description=None,
            credential_id=credential_id,
            entries=(workflows.StepData(template_id=template_id),),
            variables=(),
            hosts=(workflows.HostData(hostname="127.0.0.1", port=lab.lab_port(1)),),
        ),
        principal=owner,
    ).id


def _cron_schedule(owner: Principal, workflow_id: int) -> int:
    return schedules.create(
        schedules.ScheduleData(
            name="lab-hourly",
            workflow_id=workflow_id,
            schedule_kind=ScheduleKind.CRON,
            timezone="UTC",
            cron_expression="0 * * * *",
            misfire_policy=MisfirePolicy.SKIP,
        ),
        principal=owner,
    ).id


def _due_instant(schedule_id: int):
    """The instant the schedule says it is due, so a firing is never guessed."""
    from app.db.session import get_session_factory
    from app.models.schedule import Schedule

    session = get_session_factory()()
    try:
        row = session.get(Schedule, schedule_id)
        assert row is not None and row.next_run_at is not None
        return as_utc(row.next_run_at)
    finally:
        session.close()


def test_a_scheduled_firing_produces_a_runnable_execution(
    lab_env, settings, migrated_db
) -> None:
    resolved, credential_id = lab_env
    owner = _owner("scheduled-lab")
    marker = f"scheduled-run-{owner.user_id}"
    workflow_id = _workflow(owner, credential_id, marker=marker)
    schedule_id = _cron_schedule(owner, workflow_id)

    lease = SchedulerLease()
    assert lease.acquire().held

    firing = _due_instant(schedule_id)
    result = dispatch.dispatch(schedule_id, firing, lease=lease)
    assert result.outcome is ScheduleRunOutcome.DISPATCHED, result.detail

    execution = execution_state.get_execution(result.execution_id)
    assert execution is not None
    # Attribution is what lets the history be found from the schedule later.
    assert execution.trigger is ExecutionTrigger.SCHEDULED
    assert execution.schedule_id == schedule_id

    # The snapshot must be complete enough to actually run.
    steps = execution_state.load_steps(execution.id)
    assert [step.script.strip() for step in steps] == [f"echo {marker}"]
    assert execution_state.load_host_specs(execution.id)

    # Drive the real SSH path on the execution the firing created.
    lab.run(ExecutionRunner(settings)._run_execution(execution.id, CancelToken()))

    finished = execution_state.get_execution(execution.id)
    assert finished is not None
    assert finished.status is ExecutionStatus.SUCCEEDED, finished.error_message
    assert finished.host_runs[0].status is HostRunStatus.SUCCEEDED

    transcript = (
        settings.log_dir
        / "executions"
        / str(execution.id)
        / f"{finished.host_runs[0].id}-127.0.0.1.log"
    )
    assert transcript.exists(), "the history points at a log that was never written"
    assert marker in transcript.read_text(encoding="utf-8")


def test_a_crashed_scheduled_run_recovers_with_real_host_rows(
    lab_env, parked_runner, settings, migrated_db
) -> None:
    """Reconciliation must unblock a schedule whose host rows really exist."""
    resolved, credential_id = lab_env
    owner = _owner("scheduled-lab-recovery")
    workflow_id = _workflow(owner, credential_id, marker="recovery")
    schedule_id = _cron_schedule(owner, workflow_id)

    lease = SchedulerLease()
    assert lease.acquire().held

    firing = _due_instant(schedule_id)
    first = dispatch.dispatch(schedule_id, firing, lease=lease)
    assert first.outcome is ScheduleRunOutcome.DISPATCHED, first.detail
    execution_state.mark_execution_running(first.execution_id)

    # The process dies here; nothing else touches the row.
    blocked = dispatch.dispatch(schedule_id, firing + timedelta(hours=1), lease=lease)
    assert blocked.outcome is ScheduleRunOutcome.SKIPPED_OVERLAP

    settled = execution_state.reconcile_abandoned_executions(reason="the process stopped")
    assert settled == [first.execution_id]

    # A later instant, not a replay: re-dispatching the same firing would be
    # refused as DUPLICATE by the ledger regardless of the overlap rule.
    recovered = dispatch.dispatch(schedule_id, firing + timedelta(hours=2), lease=lease)
    assert recovered.outcome is ScheduleRunOutcome.DISPATCHED, recovered.detail

    abandoned = execution_state.get_execution(first.execution_id)
    assert abandoned is not None
    assert abandoned.status is ExecutionStatus.FAILED
    assert abandoned.host_count == 1
