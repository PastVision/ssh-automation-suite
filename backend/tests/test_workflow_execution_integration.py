"""End-to-end workflow execution against the SSH lab.

These cover the gap left by the workflow-bridge suite: ``build_execution_specs``
was well covered, but nothing exercised what the runner does *with* those specs.
Two spec kinds were loaded by nobody:

* variable specs — ``load_variable_specs`` was never called, so discovery silently
  never ran and every step rendered with no values;
* Run-As passwords — ``load_run_as_passwords`` was never called, so a
  password-mode block always sent ``None`` to ``sudo`` and failed.

Run-As substitution also deserves an end-to-end check: a wrong context runs a
step as the wrong account rather than raising.

Everything here opens a real SSH connection to the Docker test lab.
"""

from __future__ import annotations

import pytest

from app.models.enums import ExecutionStatus, HostRunStatus, StepRunStatus
from app.models.workflow import RunAsMethod
from app.services import execution_state
from app.services.execution_runner import CancelToken, ExecutionRunner
from app.services.execution_state import HostSpec, StepSpec, VariableSpec
from tests import lab_support as lab

pytestmark = pytest.mark.lab


@pytest.fixture()
def lab_env(settings, migrated_db, monkeypatch):
    resolved = lab.configure_lab_settings(monkeypatch)
    credential_id = lab.create_lab_credential(resolved)
    return resolved, credential_id


def _host(credential_id: int, index: int = 1) -> HostSpec:
    return HostSpec(
        hostname="127.0.0.1",
        port=lab.lab_port(index),
        credential_id=credential_id,
        credential_name="lab",
    )


def _step(script: str, *, name: str = "step", timeout: int = 30) -> StepSpec:
    return StepSpec(
        template_name=name,
        template_version=1,
        script=script,
        interpreter="bash",
        timeout_seconds=timeout,
    )


def _run(settings, execution_id: int):
    lab.run(ExecutionRunner(settings)._run_execution(execution_id, CancelToken()))
    return execution_state.get_execution(execution_id)


def _transcript(settings, execution) -> str:
    host = execution.host_runs[0]
    return (
        settings.log_dir
        / "executions"
        / str(execution.id)
        / f"{host.id}-127.0.0.1.log"
    ).read_text(encoding="utf-8")


def _discoveries(execution_id: int) -> list[str]:
    """Names of the discovery rows recorded for the execution's first host.

    Read inside a live session because the relationship lazy-loads: touching it on
    a detached execution raises instead of returning an empty list.
    """
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.execution import ExecutionHostRun, ExecutionVariableDiscovery

    session = get_session_factory()()
    try:
        host_run_id = session.scalar(
            select(ExecutionHostRun.id).where(ExecutionHostRun.execution_id == execution_id)
        )
        return list(
            session.scalars(
                select(ExecutionVariableDiscovery.name)
                .where(ExecutionVariableDiscovery.host_run_id == host_run_id)
                .order_by(ExecutionVariableDiscovery.name)
            ).all()
        )
    finally:
        session.close()


def _transitions(execution_id: int) -> list[tuple[int, str, str]]:
    """``(position, target, status)`` for each Run-As transition on the first host.

    The identity a run entered lives on a transition row, not on the step row:
    one block can govern several steps, so recording it per step would duplicate
    the same fact and let the two disagree.
    """
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.execution import ExecutionHostRun, ExecutionRunAsTransition

    session = get_session_factory()()
    try:
        host_run_id = session.scalar(
            select(ExecutionHostRun.id).where(ExecutionHostRun.execution_id == execution_id)
        )
        rows = session.scalars(
            select(ExecutionRunAsTransition)
            .where(ExecutionRunAsTransition.host_run_id == host_run_id)
            .order_by(ExecutionRunAsTransition.position)
        ).all()
        return [(r.position, r.target_username, r.status) for r in rows]
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Variable discovery
# --------------------------------------------------------------------------- #


def test_a_discovered_variable_reaches_the_step_that_uses_it(lab_env):
    """The whole point of discovery: the value is computed per host and then
    available to every step on that host, without being stored anywhere."""
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="variables",
        steps=[_step('echo "value={{NODE_KIND}}"\n')],
        hosts=[_host(credential_id)],
        variables=[
            VariableSpec(
                name="NODE_KIND",
                discovery_command="echo lab-node",
                timeout_seconds=20,
            )
        ],
    )

    execution = _run(settings, execution_id)

    assert execution.status is ExecutionStatus.SUCCEEDED
    host = execution.host_runs[0]
    assert host.status is HostRunStatus.SUCCEEDED
    assert host.step_runs[0].status is StepRunStatus.SUCCEEDED

    # The rendered value is what the step actually ran with, so the transcript is
    # the evidence; the step row records only the outcome.
    assert "value=lab-node" in _transcript(settings, execution)


def test_discovery_is_recorded_without_leaking_the_value(lab_env):
    """Operators need to see that discovery ran; the host's data must not end up
    in a shared log, so only metadata is recorded."""
    settings, credential_id = lab_env
    secretish = "host-specific-value"
    execution_id = execution_state.create_execution(
        workflow_name="variables",
        steps=[_step("true\n")],
        hosts=[_host(credential_id)],
        variables=[
            VariableSpec(
                name="SECRETISH",
                discovery_command=f"echo {secretish}",
                timeout_seconds=20,
            )
        ],
    )

    execution = _run(settings, execution_id)
    assert execution.status is ExecutionStatus.SUCCEEDED

    assert _discoveries(execution_id) == ["SECRETISH"]

    transcript = _transcript(settings, execution)
    assert "discovering variable" in transcript
    assert secretish not in transcript


def test_a_failing_discovery_stops_the_host_before_any_step(lab_env):
    """Running steps with an undefined variable is worse than not running them:
    the script may do something destructive with an empty value."""
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="variables",
        steps=[_step("echo should-not-run\n")],
        hosts=[_host(credential_id)],
        variables=[
            VariableSpec(
                name="BROKEN",
                discovery_command="exit 3",
                timeout_seconds=20,
            )
        ],
    )

    execution = _run(settings, execution_id)

    host = execution.host_runs[0]
    assert host.status is HostRunStatus.FAILED
    # The step row exists from the start, but it must never have been executed.
    assert all(run.status is not StepRunStatus.SUCCEEDED for run in host.step_runs)
    assert "should-not-run" not in _transcript(settings, execution)
    assert execution.status is ExecutionStatus.FAILED


# --------------------------------------------------------------------------- #
# Run-As
# --------------------------------------------------------------------------- #


def test_a_step_after_a_run_as_block_executes_as_the_target(lab_env):
    """Attribution has to survive the hop into the runner, not just the spec.

    The lab image has a ``deploy`` account with passwordless sudo, so the
    substituted script can be checked by the identity it reports.
    """
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="run-as",
        steps=[
            _step("whoami\n", name="before"),
            StepSpec(
                template_name="after",
                template_version=1,
                script="whoami\n",
                interpreter="bash",
                timeout_seconds=30,
                run_as_username="deploy",
                run_as_method=RunAsMethod.NOPASSWD,
                run_as_position=0,
            ),
        ],
        hosts=[_host(credential_id)],
    )

    execution = _run(settings, execution_id)
    assert execution.status is ExecutionStatus.SUCCEEDED

    transcript = _transcript(settings, execution)
    # The block is positional: the first step keeps the primary user, the second
    # runs as the target. Getting this backwards is a privilege bug, not a typo.
    assert "deploy" in transcript
    assert [run.status for run in execution.host_runs[0].step_runs] == [
        StepRunStatus.SUCCEEDED,
        StepRunStatus.SUCCEEDED,
    ]
    assert _transitions(execution_id) == [(0, "deploy", StepRunStatus.SUCCEEDED)]


def test_a_run_as_password_is_decrypted_for_the_run_and_never_logged(lab_env):
    """The password travels from the workflow row, not from the persisted specs.

    It must reach ``sudo`` (so the step can run) while never being written to the
    transcript. This is the path that was silently broken: the loader was never
    called, so a password-mode block always sent ``None``.
    """
    from app.db.session import get_session_factory
    from app.models.workflow import RunAsMethod as ModelMethod
    from app.models.workflow import Workflow, WorkflowRunAsBlock
    from app.services import execution_state as state

    settings, credential_id = lab_env
    password = "Sup3rSecretSudo"

    # The loader reads blocks from the execution's workflow, so one must exist.
    session = get_session_factory()()
    try:
        workflow = Workflow(
            name="run-as-password",
            owner_id=lab.ensure_test_owner(),
            credential_id=credential_id,
            row_version=1,
        )
        session.add(workflow)
        session.flush()
        from app.core.security import encrypt_secret

        session.add(
            WorkflowRunAsBlock(
                workflow_id=workflow.id,
                position=0,
                target_username="deploy",
                method=ModelMethod.PASSWORD,
                password_encrypted=encrypt_secret(password),
            )
        )
        session.commit()
        workflow_id = workflow.id
    finally:
        session.close()

    execution_id = state.create_execution(
        workflow_name="run-as",
        steps=[
            StepSpec(
                template_name="pw",
                template_version=1,
                script="whoami\n",
                interpreter="bash",
                timeout_seconds=30,
                run_as_username="deploy",
                run_as_method=RunAsMethod.PASSWORD,
                run_as_position=0,
            )
        ],
        hosts=[_host(credential_id)],
        workflow_id=workflow_id,
    )

    # The loader must actually produce the password, or sudo receives nothing.
    assert state.load_run_as_passwords(execution_id) == {0: password}

    execution = _run(settings, execution_id)

    transcript = (
        settings.log_dir
        / "executions"
        / str(execution.id)
        / f"{execution.host_runs[0].id}-127.0.0.1.log"
    ).read_text(encoding="utf-8")
    assert password not in transcript


def test_a_password_block_whose_workflow_is_gone_sends_no_password(lab_env):
    """Removing the block must not make the step run with a stale or empty value;
    it must fail clearly instead of guessing."""
    from app.services import execution_state as state

    settings, credential_id = lab_env
    execution_id = state.create_execution(
        workflow_name="run-as",
        steps=[
            StepSpec(
                template_name="pw",
                template_version=1,
                script="whoami\n",
                interpreter="bash",
                timeout_seconds=30,
                run_as_username="deploy",
                run_as_method=RunAsMethod.PASSWORD,
                run_as_position=0,
            )
        ],
        hosts=[_host(credential_id)],
    )

    assert state.load_run_as_passwords(execution_id) == {}

    execution = _run(settings, execution_id)
    # A password-mode step with no password cannot succeed.
    assert execution.host_runs[0].status is HostRunStatus.FAILED
