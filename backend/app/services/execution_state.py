"""Execution persistence.

Everything that writes execution state lives here so the runner stays focused on
orchestration. Two rules are enforced:

* SQLite holds **metadata only**. Transcript bytes go through
  ``ExecutionLogStore``; the database records paths.
* Every write happens in a short-lived session. A long-running SSH connection
  must never hold a SQLite transaction open, because SQLite serialises writers
  and that would stall every other host workflow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.session import get_session_factory
from app.db.types import utcnow
from app.models.enums import (
    ExecutionStatus,
    ExecutionTrigger,
    HostRunStatus,
    StepDecision,
    StepRunStatus,
)
from app.models.execution import (
    Execution,
    ExecutionHostRun,
    ExecutionRunAsTransition,
    ExecutionStepRun,
    ExecutionTemplateSnapshot,
    ExecutionVariableDiscovery,
)
from app.models.template import TemplateVersion
from app.models.workflow import RunAsMethod
from app.services.conditions import EvaluationResult, StepRule
from app.services.log_store import ExecutionLogStore

logger = logging.getLogger(__name__)


class ExecutionNotFound(LookupError):
    pass


class InvalidExecutionState(RuntimeError):
    """Raised when a transition is not legal for the execution's current state."""


@dataclass(frozen=True)
class StepSpec:
    """One template version frozen into the execution."""

    template_name: str
    template_version: int
    script: str
    interpreter: str
    timeout_seconds: int
    template_id: int | None = None
    template_version_id: int | None = None
    # Defaults to ``CONTINUE``, which is exactly the behaviour a step had before
    # execution rules existed, so every existing caller keeps working unchanged.
    rule: StepRule = field(default_factory=StepRule)
    # Which Run-As identity this step runs under. ``None`` means the primary SSH
    # user. ``run_as_position`` identifies the workflow block that governs it, so
    # history can group child steps under the transition that produced them.
    run_as_username: str | None = None
    run_as_method: RunAsMethod | None = None
    run_as_position: int | None = None


@dataclass(frozen=True)
class VariableSpec:
    """A workflow variable discovery command frozen into the execution."""

    name: str
    discovery_command: str
    timeout_seconds: int


@dataclass(frozen=True)
class HostSpec:
    hostname: str
    port: int
    credential_id: int
    credential_name: str
    # Carried here so a host workflow never has to re-query per step. It is not a
    # secret; the private key is resolved separately and never stored on the spec.
    username: str = ""


def _session() -> Session:
    return get_session_factory()()


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #

def create_execution(
    *,
    workflow_name: str,
    steps: list[StepSpec],
    hosts: list[HostSpec],
    variables: list[VariableSpec] | None = None,
    trigger: ExecutionTrigger = ExecutionTrigger.MANUAL,
    workflow_id: int | None = None,
    max_concurrent_hosts: int = 20,
    created_by_id: int | None = None,
    schedule_id: int | None = None,
    schedule_name: str | None = None,
    scheduled_for: datetime | None = None,
    session: Session | None = None,
) -> int:
    """Persist a PENDING execution with frozen template snapshots.

    Snapshots are written here, before any SSH work, so a template edited while
    the run is in flight cannot change what the run executes. Step rows are
    created up front for every host so skip/fail handling is a status update.

    Variable discovery rows are also created up front, one per variable per host,
    so a host that fails during discovery still has a row explaining which
    variable it was on rather than an empty history.

    ``session`` lets a caller compose this insert into its own transaction. The
    scheduled dispatcher needs that: the lease fence, the ledger outcome and this
    row must commit together, and the partial unique index on ``schedule_id`` only
    protects anything if the insert and the fence share a transaction. When a
    session is supplied it is flushed but never committed or closed here, and the
    log directory is *not* created until the caller's commit succeeds.
    """
    variables = variables or []
    owns_session = session is None
    session = session or _session()
    log_store = ExecutionLogStore()
    prepared_dir: Path | None = None
    try:
        execution = Execution(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            status=ExecutionStatus.PENDING,
            trigger=trigger,
            max_concurrent_hosts=max_concurrent_hosts,
            host_count=len(hosts),
            created_by_id=created_by_id,
            schedule_id=schedule_id,
            schedule_name=schedule_name,
            scheduled_for=scheduled_for,
        )
        session.add(execution)
        session.flush()

        snapshots: list[ExecutionTemplateSnapshot] = []
        for position, step in enumerate(steps):
            snapshot = ExecutionTemplateSnapshot(
                execution_id=execution.id,
                template_id=step.template_id,
                template_version_id=step.template_version_id,
                position=position,
                template_name=step.template_name,
                template_version=step.template_version,
                script=step.script,
                script_checksum=TemplateVersion.compute_checksum(step.script),
                interpreter=step.interpreter,
                timeout_seconds=step.timeout_seconds,
                run_as_username=step.run_as_username,
                run_as_method=step.run_as_method,
                run_as_position=step.run_as_position,
            )
            # The rule is frozen with the script so a template edited (or deleted)
            # while the run is in flight cannot change what this run decided.
            snapshot.set_rule(step.rule)
            snapshots.append(snapshot)
            session.add(snapshot)
        session.flush()

        for host in hosts:
            host_run = ExecutionHostRun(
                execution_id=execution.id,
                hostname=host.hostname,
                port=host.port,
                credential_name=host.credential_name,
                status=HostRunStatus.PENDING,
            )
            session.add(host_run)
            session.flush()
            for position, snapshot in enumerate(snapshots):
                session.add(
                    ExecutionStepRun(
                        host_run_id=host_run.id,
                        snapshot_id=snapshot.id,
                        position=position,
                        template_name=snapshot.template_name,
                        status=StepRunStatus.PENDING,
                    )
                )
            for variable_position, variable in enumerate(variables):
                session.add(
                    ExecutionVariableDiscovery(
                        host_run_id=host_run.id,
                        name=variable.name,
                        discovery_command=variable.discovery_command,
                        timeout_seconds=variable.timeout_seconds,
                        position=variable_position,
                        status=StepRunStatus.PENDING,
                    )
                )
            seen_blocks: set[int] = set()
            for step in steps:
                if step.run_as_position is None or step.run_as_position in seen_blocks:
                    continue
                seen_blocks.add(step.run_as_position)
                session.add(
                    ExecutionRunAsTransition(
                        host_run_id=host_run.id,
                        position=step.run_as_position,
                        target_username=step.run_as_username or "",
                        method=step.run_as_method or RunAsMethod.NOPASSWD,
                        status=StepRunStatus.PENDING,
                    )
                )

        store = ExecutionLogStore()
        prepared_dir = store.prepare_execution_dir(execution.id)
        execution.log_path = store.relative(prepared_dir)
        if owns_session:
            session.commit()
        else:
            session.flush()
        execution_id = execution.id
    except Exception:
        # Only an owned session may be rolled back here. When the caller supplied
        # one it is running inside a SAVEPOINT, and rolling back the outer
        # transaction would discard the caller's ledger row too.
        if owns_session:
            session.rollback()
        if prepared_dir is not None:
            # The row is gone, so the empty directory would be an orphan. The
            # flush precedes the commit, so a committed row can never reach here.
            ExecutionLogStore().delete_execution_dir(execution_id)
        raise
    finally:
        if owns_session:
            session.close()

    if owns_session:
        write_execution_manifest(execution_id)
    return execution_id


def discard_execution_dir(execution_id: int) -> None:
    """Remove an execution's log directory. Used when a composed transaction fails."""
    ExecutionLogStore().delete_execution_dir(execution_id)


def write_execution_manifest(execution_id: int) -> None:
    """Write ``manifest.json`` beside the transcripts.

    Public because a caller that composes the insert into its own transaction must
    call it *after* committing: the row does not exist before then.
    """
    _write_manifest(execution_id)


def _write_manifest(execution_id: int) -> None:
    """Record what the run was configured to do, beside its transcripts."""
    store = ExecutionLogStore()
    session = _session()
    try:
        execution = session.get(Execution, execution_id)
        if execution is None:
            return
        store.write_manifest(
            execution_id,
            {
                "execution_id": execution_id,
                "workflow_name": execution.workflow_name,
                "trigger": execution.trigger.value,
                "max_concurrent_hosts": execution.max_concurrent_hosts,
                "hosts": [
                    {"hostname": h.hostname, "port": h.port, "credential": h.credential_name}
                    for h in sorted(execution.host_runs, key=lambda r: r.id)
                ],
                "steps": [
                    {
                        "position": s.position,
                        "template": s.template_name,
                        "version": s.template_version,
                        "interpreter": s.interpreter,
                        "timeout_seconds": s.timeout_seconds,
                        "checksum": s.script_checksum,
                        "action": s.action,
                        "condition": _manifest_condition(s),
                    }
                    for s in execution.snapshots
                ],
            },
        )
    finally:
        session.close()


def _manifest_condition(snapshot: ExecutionTemplateSnapshot) -> dict[str, object] | None:
    """Describe a snapshot's condition for the on-disk manifest.

    Omitted entirely for an unconditional step, so the manifest of a plain
    workflow stays comparable with what earlier milestones wrote.
    """
    rule = snapshot.to_rule()
    if rule.condition is None:
        return None
    condition = rule.condition
    return {
        "stream": condition.stream.value,
        "location": condition.location.value,
        "operator": condition.operator.value,
        "expected": condition.expected,
        "case_sensitive": condition.case_sensitive,
        "line_number": condition.line_number,
        "anchor": condition.anchor,
    }


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def load_steps(execution_id: int) -> list[StepSpec]:
    session = _session()
    try:
        execution = session.get(Execution, execution_id)
        if execution is None:
            raise ExecutionNotFound(execution_id)
        return [
            StepSpec(
                template_name=s.template_name,
                template_version=s.template_version,
                script=s.script,
                interpreter=s.interpreter,
                timeout_seconds=s.timeout_seconds,
                template_id=s.template_id,
                template_version_id=s.template_version_id,
                rule=s.to_rule(),
                run_as_username=s.run_as_username,
                run_as_method=s.run_as_method,
                run_as_position=s.run_as_position,
            )
            for s in execution.snapshots
        ]
    finally:
        session.close()


def load_variable_specs(execution_id: int) -> list[VariableSpec]:
    """Discovery definitions frozen for this execution, ordered for determinism.

    Read from the per-host discovery rows rather than the live workflow, so a
    variable edited mid-run cannot change what an in-flight host discovers.
    """
    session = _session()
    try:
        rows = session.scalars(
            select(ExecutionVariableDiscovery)
            .join(
                ExecutionHostRun,
                ExecutionHostRun.id == ExecutionVariableDiscovery.host_run_id,
            )
            .where(ExecutionHostRun.execution_id == execution_id)
            .order_by(ExecutionVariableDiscovery.position, ExecutionVariableDiscovery.id)
        ).all()
        seen: dict[str, VariableSpec] = {}
        for row in rows:
            seen.setdefault(
                row.name,
                VariableSpec(
                    name=row.name,
                    discovery_command=row.discovery_command,
                    timeout_seconds=row.timeout_seconds,
                ),
            )
        return list(seen.values())
    finally:
        session.close()


def load_host_specs(execution_id: int) -> list[tuple[int, HostSpec]]:
    """Return ``(host_run_id, spec)`` pairs, ordered for deterministic output."""
    session = _session()
    try:
        rows = session.scalars(
            select(ExecutionHostRun)
            .where(ExecutionHostRun.execution_id == execution_id)
            .order_by(ExecutionHostRun.id)
        ).all()
        specs = []
        for row in rows:
            credential_id, username = _credential_for(session, row)
            specs.append(
                (
                    row.id,
                    HostSpec(
                        hostname=row.hostname,
                        port=row.port,
                        credential_id=credential_id,
                        credential_name=row.credential_name or "",
                        username=username,
                    ),
                )
            )
        return specs
    finally:
        session.close()


def _credential_for(session: Session, host_run: ExecutionHostRun) -> tuple[int, str]:
    """Resolve the credential id and username a host row was snapshotted against.

    ``ExecutionHostRun`` stores only ``credential_name`` so history survives a
    credential rename. The id is therefore resolved through the workflow at run
    time rather than duplicated onto every host row.
    """
    from app.models.credential import SshCredential
    from app.models.workflow import Workflow

    execution = session.get(Execution, host_run.execution_id)
    if execution is not None and execution.workflow_id is not None:
        workflow = session.get(Workflow, execution.workflow_id)
        if workflow is not None and workflow.credential_id is not None:
            credential = session.get(SshCredential, workflow.credential_id)
            if credential is not None:
                return credential.id, credential.username
    credential = session.scalar(
        select(SshCredential).where(SshCredential.name == host_run.credential_name)
    )
    if credential is not None:
        return credential.id, credential.username
    raise InvalidExecutionState(
        f"No credential could be resolved for host {host_run.hostname!r}."
    )


def list_executions(
    *,
    created_by_id: int,
    limit: int = 50,
    offset: int = 0,
    status: ExecutionStatus | None = None,
    workflow_id: int | None = None,
) -> tuple[list[Execution], int]:
    """History for one user, newest first, with the total matching count.

    Scoped to ``created_by_id`` here rather than in the route so a caller cannot
    forget the ownership filter and leak another user's runs. Host and step rows are
    deliberately *not* eager-loaded: the summary row does not need them, and loading
    them for a page of runs would multiply the query cost for no benefit.
    """
    session = _session()
    try:
        filters = [Execution.created_by_id == created_by_id]
        if status is not None:
            filters.append(Execution.status == status)
        if workflow_id is not None:
            filters.append(Execution.workflow_id == workflow_id)

        total = session.scalar(
            select(func.count()).select_from(Execution).where(*filters)
        )
        rows = list(
            session.scalars(
                select(Execution)
                .where(*filters)
                .order_by(Execution.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return rows, int(total or 0)
    finally:
        session.close()


def get_execution(execution_id: int) -> Execution | None:
    session = _session()
    try:
        return session.scalars(
            select(Execution)
            .where(Execution.id == execution_id)
            .options(
                selectinload(Execution.host_runs).selectinload(ExecutionHostRun.step_runs),
                selectinload(Execution.snapshots),
            )
        ).first()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #

def mark_execution_running(execution_id: int) -> None:
    with _write_session() as session:
        execution = session.get(Execution, execution_id)
        if execution is None:
            raise ExecutionNotFound(execution_id)
        if execution.status is not ExecutionStatus.PENDING:
            raise InvalidExecutionState(
                f"Execution {execution_id} is {execution.status}, expected pending"
            )
        execution.status = ExecutionStatus.RUNNING
        execution.started_at = utcnow()


def mark_host_running(host_run_id: int) -> None:
    with _write_session() as session:
        host_run = session.get(ExecutionHostRun, host_run_id)
        if host_run is None:
            return
        host_run.status = HostRunStatus.RUNNING
        host_run.started_at = utcnow()


def mark_step_running(step_run_id: int) -> None:
    with _write_session() as session:
        step_run = session.get(ExecutionStepRun, step_run_id)
        if step_run is None:
            return
        step_run.status = StepRunStatus.RUNNING
        step_run.started_at = utcnow()


def finish_step(
    step_run_id: int,
    *,
    status: StepRunStatus,
    exit_code: int | None,
    duration_ms: int | None,
) -> None:
    with _write_session() as session:
        step_run = session.get(ExecutionStepRun, step_run_id)
        if step_run is None:
            return
        step_run.status = status
        step_run.exit_code = exit_code
        step_run.duration_ms = duration_ms
        step_run.finished_at = utcnow()


def skip_step(step_run_id: int, *, reason: str) -> None:
    """Mark a step SKIPPED because a preceding ``SKIP_NEXT_IF`` rule matched.

    A skipped step never connects, so it has no exit code or duration; the reason
    is what the UI shows instead.
    """
    with _write_session() as session:
        step_run = session.get(ExecutionStepRun, step_run_id)
        if step_run is None:
            return
        step_run.status = StepRunStatus.SKIPPED
        step_run.decision = StepDecision.SKIP_NEXT
        step_run.finished_at = utcnow()
        step_run.evaluation_error = _clip_error(reason)


def record_rule_outcome(
    step_run_id: int,
    *,
    rule: StepRule,
    result: EvaluationResult | None,
    decision: StepDecision,
    evaluation_error: str | None = None,
) -> None:
    """Persist what the rule asked for, what the evaluator saw, and what happened.

    The condition *configuration* is copied here even though the snapshot holds
    it: a host's step row must explain itself in one query, and the columns are
    small scalars. The transcript text itself is never copied -- ``observed`` is
    a clipped excerpt of at most a few hundred characters.
    """
    condition = rule.condition
    with _write_session() as session:
        step_run = session.get(ExecutionStepRun, step_run_id)
        if step_run is None:
            return
        step_run.decision = decision
        step_run.rule_action = rule.action
        if condition is not None:
            step_run.rule_stream = condition.stream
            step_run.rule_location = condition.location
            step_run.rule_operator = condition.operator
            step_run.rule_anchor = condition.anchor
            step_run.rule_line_number = condition.line_number
            step_run.rule_case_sensitive = condition.case_sensitive
        if result is not None:
            step_run.condition_matched = result.matched
            step_run.anchor_found = result.anchor_found
            step_run.evaluated_line = result.evaluated_line
            step_run.observed = result.observed
        if evaluation_error:
            step_run.evaluation_error = _clip_error(evaluation_error)


def _clip_error(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def finish_host(
    host_run_id: int,
    *,
    status: HostRunStatus,
    error_message: str | None = None,
    failed_step_position: int | None = None,
    cancelled_steps: bool = False,
) -> None:
    """Finalise a host row and any of its steps still pending.

    Pending steps are marked SKIPPED (fail-fast) or CANCELLED (interrupted) so no
    step row is left claiming to be PENDING after its host has finished.
    """
    with _write_session() as session:
        host_run = session.get(ExecutionHostRun, host_run_id)
        if host_run is None:
            return
        host_run.status = status
        host_run.error_message = error_message
        host_run.failed_step_position = failed_step_position
        host_run.finished_at = utcnow()
        terminal = StepRunStatus.CANCELLED if cancelled_steps else StepRunStatus.SKIPPED
        for step_run in host_run.step_runs:
            if step_run.status in (StepRunStatus.PENDING, StepRunStatus.RUNNING):
                step_run.status = terminal
                step_run.finished_at = utcnow()


def finish_execution(execution_id: int, *, status: ExecutionStatus,
                     error_message: str | None = None) -> None:
    """Persist the final state and the derived host counters.

    ``error_message`` doubles as the cancellation marker; per-host reasons are
    preserved on their own rows.
    """
    with _write_session() as session:
        execution = session.get(Execution, execution_id)
        if execution is None:
            return
        hosts = session.scalars(
            select(ExecutionHostRun).where(ExecutionHostRun.execution_id == execution_id)
        ).all()
        execution.host_count = len(hosts)
        # TERMINATED is a *successful* early stop, so it counts with the
        # succeeded hosts. It keeps its own status so the history can still say
        # why the workflow ended before its last step.
        execution.succeeded_host_count = sum(
            1
            for h in hosts
            if h.status in (HostRunStatus.SUCCEEDED, HostRunStatus.TERMINATED)
        )
        execution.failed_host_count = sum(
            1 for h in hosts if h.status is HostRunStatus.FAILED
        )
        execution.status = status
        execution.error_message = error_message
        execution.finished_at = utcnow()


def set_execution_log_path(execution_id: int, relative_path: str) -> None:
    with _write_session() as session:
        execution = session.get(Execution, execution_id)
        if execution is not None:
            execution.log_path = relative_path


def set_host_log_path(host_run_id: int, relative_path: str) -> None:
    with _write_session() as session:
        host_run = session.get(ExecutionHostRun, host_run_id)
        if host_run is not None:
            host_run.log_path = relative_path


def host_status(host_run_id: int) -> HostRunStatus | None:
    session = _session()
    try:
        host_run = session.get(ExecutionHostRun, host_run_id)
        return host_run.status if host_run else None
    finally:
        session.close()


def execution_status(execution_id: int) -> ExecutionStatus | None:
    session = _session()
    try:
        execution = session.get(Execution, execution_id)
        return execution.status if execution else None
    finally:
        session.close()


def record_discovery(
    host_run_id: int,
    name: str,
    *,
    status: StepRunStatus,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    value_length: int | None = None,
    detail: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    """Record the outcome of one variable discovery on one host.

    Deliberately does not accept the discovered *value*: it is used to render the
    run and then dropped. Keeping it out of the schema means it cannot leak into
    history through an oversight.
    """
    session = _session()
    try:
        row = session.scalar(
            select(ExecutionVariableDiscovery).where(
                ExecutionVariableDiscovery.host_run_id == host_run_id,
                ExecutionVariableDiscovery.name == name,
            )
        )
        if row is None:
            return
        row.status = status
        row.exit_code = exit_code
        row.duration_ms = duration_ms
        row.value_length = value_length
        row.detail = _clip_error(detail) if detail else None
        row.started_at = started_at
        row.finished_at = finished_at or utcnow()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_transition(
    host_run_id: int,
    position: int,
    *,
    status: StepRunStatus,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    detail: str | None = None,
    finished: bool = True,
) -> None:
    """Record a Run-As transition attempt.

    ``detail`` must carry only a bounded, credential-free reason. The password
    never reaches this function, so it cannot reach the database or a log.
    """
    session = _session()
    try:
        row = session.scalar(
            select(ExecutionRunAsTransition).where(
                ExecutionRunAsTransition.host_run_id == host_run_id,
                ExecutionRunAsTransition.position == position,
            )
        )
        if row is None:
            return
        row.status = status
        row.exit_code = exit_code
        row.duration_ms = duration_ms
        row.detail = _clip_error(detail) if detail else None
        if status is StepRunStatus.RUNNING:
            row.started_at = row.started_at or utcnow()
        if finished:
            row.finished_at = utcnow()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def load_run_as_passwords(execution_id: int) -> dict[int, str]:
    """Decrypt the Run-As passwords an execution needs, keyed by block position.

    Read from the *workflow* the execution came from, because the password is
    deliberately never copied into history. An execution whose workflow has since
    been deleted, or whose block was removed, simply has no password here; the
    password-mode step then fails with a clear message rather than running as the
    wrong identity.

    The result is held only for the lifetime of the run (see ``ExecutionRunner``).
    """
    from app.core.security import decrypt_secret
    from app.models.workflow import WorkflowRunAsBlock

    session = _session()
    try:
        execution = session.get(Execution, execution_id)
        if execution is None or execution.workflow_id is None:
            return {}
        blocks = session.scalars(
            select(WorkflowRunAsBlock).where(
                WorkflowRunAsBlock.workflow_id == execution.workflow_id
            )
        ).all()
        passwords: dict[int, str] = {}
        for block in blocks:
            if block.password_encrypted:
                passwords[block.position] = decrypt_secret(
                    block.password_encrypted
                ).decode("utf-8")
        return passwords
    finally:
        session.close()


def step_run_id(host_run_id: int, position: int) -> int | None:
    session = _session()
    try:
        return session.scalar(
            select(ExecutionStepRun.id).where(
                ExecutionStepRun.host_run_id == host_run_id,
                ExecutionStepRun.position == position,
            )
        )
    finally:
        session.close()


def pending_host_run_ids(execution_id: int) -> list[int]:
    session = _session()
    try:
        return list(
            session.scalars(
                select(ExecutionHostRun.id).where(
                    ExecutionHostRun.execution_id == execution_id,
                    ExecutionHostRun.status == HostRunStatus.PENDING,
                )
            ).all()
        )
    finally:
        session.close()


class _write_session:
    """Context manager: commit on success, roll back on failure, always close."""

    def __enter__(self) -> Session:
        self._session = _session()
        return self._session

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
        return False


def latest_execution_started_at() -> datetime | None:
    """Used by the scheduler milestone later; kept here so the query has one home."""
    session = _session()
    try:
        return session.scalar(select(Execution.started_at).order_by(Execution.id.desc()))
    finally:
        session.close()
