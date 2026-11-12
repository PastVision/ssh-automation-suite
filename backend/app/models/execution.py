"""Execution history.

Metadata lives in SQLite; the actual stdout/stderr transcripts live on the
filesystem under ``logs/executions/<execution_id>/``. Only paths are stored.

Two snapshot mechanisms keep history readable after edits:
  * ``ExecutionTemplateSnapshot`` copies the exact script text per execution.
  * Host/step rows copy names and positions instead of joining to live tables.

Variable *values* are never stored: ``ExecutionVariableDiscovery`` records which
variable was discovered on which host, how long it took and whether it succeeded.
The value itself lives only in that host's in-process execution context, because
a discovered value can be machine configuration that must not become permanent
history in a second place.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import enum_column, timestamp_column
from app.models.enums import (
    ConditionLocation,
    ExecutionStatus,
    ExecutionTrigger,
    HostRunStatus,
    LogVisibility,
    MatchOperator,
    OutputStream,
    RunAsMethod,
    StepAction,
    StepDecision,
    StepRunStatus,
    VariableSource,
)
from app.models.step_rule import RuleColumns


class Execution(Base):
    """One run of a workflow across all of its hosts."""

    __tablename__ = "executions"
    __table_args__ = (
        # Overlap policy ``skip``, enforced by the database rather than by a
        # check-then-insert: at most one non-terminal run per schedule. Manual
        # runs have no schedule and never collide. This is what removes the
        # time-of-check/time-of-use race; the dispatcher inserts and handles
        # IntegrityError instead of looking first.
        Index(
            "uq_executions_one_active_per_schedule",
            "schedule_id",
            unique=True,
            sqlite_where=text(
                "schedule_id IS NOT NULL AND status IN ('pending', 'running')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Who started the run. Enforced when reading or cancelling an execution so one
    # user cannot observe or stop another user's SSH work. Nullable so history
    # survives account deletion (SET NULL) without losing the row.
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # NO ACTION, not CASCADE: schedules are soft-deleted, and a hard cascade here
    # would delete execution history along with them.
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="NO ACTION"), nullable=True, index=True
    )
    # Snapshot so a scheduled run stays readable after the schedule row is gone
    # from view, matching the ``workflow_name`` contract above.
    schedule_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # The logical firing time this run was dispatched for, in UTC. Distinct from
    # ``created_at``, which is when the row was written.
    scheduled_for: Mapped[datetime | None] = mapped_column(nullable=True)

    workflow_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(
        enum_column(ExecutionStatus, "execution_status"),
        nullable=False,
        default=ExecutionStatus.PENDING,
        index=True,
    )
    trigger: Mapped[ExecutionTrigger] = mapped_column(
        enum_column(ExecutionTrigger, "execution_trigger"),
        nullable=False,
        default=ExecutionTrigger.MANUAL,
    )

    # Ceiling applied to this run; recorded so history explains its own behaviour
    # even if the global setting changes later.
    max_concurrent_hosts: Mapped[int] = mapped_column(Integer, nullable=False, default=20)

    host_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_host_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_host_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()

    # -- cancellation ------------------------------------------------------- #
    # Who asked to stop this run and when. Recorded even if the run finished
    # first, so "cancel raced completion" is visible in history rather than
    # looking like a cancellation that silently did nothing.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancel_requested_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancel_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # -- log visibility ----------------------------------------------------- #
    # Governs transcripts and step output only. Defaults to the workflow owner's
    # privacy, so no historical run becomes readable just because this column
    # was introduced.
    log_visibility: Mapped[LogVisibility] = mapped_column(
        enum_column(LogVisibility, "log_visibility"),
        nullable=False,
        default=LogVisibility.PRIVATE,
    )

    # Root directory of this run's transcripts, relative to the backend directory.
    log_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    host_runs: Mapped[list[ExecutionHostRun]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[ExecutionTemplateSnapshot]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="ExecutionTemplateSnapshot.position",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Execution id={self.id} workflow={self.workflow_name!r} status={self.status}>"


class ExecutionTemplateSnapshot(Base, RuleColumns):
    """Frozen copy of a template version, taken when the execution is created."""

    __tablename__ = "execution_template_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Kept for lineage only; nulled if the template is deleted.
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("templates.id", ondelete="SET NULL"), nullable=True
    )
    template_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("template_versions.id", ondelete="SET NULL"), nullable=True
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    template_name: Mapped[str] = mapped_column(String(120), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    script_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    interpreter: Mapped[str] = mapped_column(String(40), nullable=False, default="bash")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    # Which Run-As identity this step runs under, frozen at execution creation.
    # NULL means the primary SSH user. The password is deliberately absent: it is
    # resolved from the workflow's block at run time and never copied into
    # history.
    run_as_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    run_as_method: Mapped[RunAsMethod | None] = mapped_column(
        enum_column(RunAsMethod, "run_as_method"), nullable=True
    )
    # The workflow block that governs this step, so history can group the child
    # templates under the transition that produced them. ``NULL`` means the step
    # ran as the primary SSH user.
    run_as_position: Mapped[int | None] = mapped_column(Integer, nullable=True)

    execution: Mapped[Execution] = relationship(back_populates="snapshots")


class ExecutionHostRun(Base):
    """Per-host state. One row per target host, independent fail-fast unit."""

    __tablename__ = "execution_host_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    credential_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    status: Mapped[HostRunStatus] = mapped_column(
        enum_column(HostRunStatus, "host_run_status"),
        nullable=False,
        default=HostRunStatus.PENDING,
        index=True,
    )
    # Position of the step that failed, so the UI can point at it directly.
    failed_step_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # ``logs/executions/<execution_id>/<host_run_id>.log``.
    log_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    execution: Mapped[Execution] = relationship(back_populates="host_runs")
    step_runs: Mapped[list[ExecutionStepRun]] = relationship(
        back_populates="host_run",
        cascade="all, delete-orphan",
        order_by="ExecutionStepRun.position",
    )
    variable_discoveries: Mapped[list[ExecutionVariableDiscovery]] = relationship(
        back_populates="host_run",
        cascade="all, delete-orphan",
        order_by="ExecutionVariableDiscovery.name",
    )
    run_as_transitions: Mapped[list[ExecutionRunAsTransition]] = relationship(
        back_populates="host_run",
        cascade="all, delete-orphan",
        order_by="ExecutionRunAsTransition.position",
    )


class ExecutionStepRun(Base):
    """Per-host, per-template result, including the exit code.

    Rule *outcomes* are recorded here rather than the rule itself: the rule is
    identical for every host and already frozen on the snapshot, so copying its
    condition columns onto every host row would be pure duplication. What differs
    per host is what the evaluator saw and what the engine then did, and that is
    what these columns hold. ``observed`` is a short excerpt, never the whole
    stream, so history cannot become a second copy of the transcript.
    """

    __tablename__ = "execution_step_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_run_id: Mapped[int] = mapped_column(
        ForeignKey("execution_host_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("execution_template_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    template_name: Mapped[str] = mapped_column(String(120), nullable=False)

    status: Mapped[StepRunStatus] = mapped_column(
        enum_column(StepRunStatus, "step_run_status"),
        nullable=False,
        default=StepRunStatus.PENDING,
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # -- execution rule outcome (NULL when the step had no conditional rule) -- #
    # What the engine did after evaluating this step's rule.
    decision: Mapped[StepDecision | None] = mapped_column(
        enum_column(StepDecision, "step_decision"), nullable=True
    )
    # The condition actually evaluated, denormalised from the snapshot so one step
    # row in the UI explains itself without a second lookup.
    rule_action: Mapped[StepAction | None] = mapped_column(
        enum_column(StepAction, "step_action"), nullable=True
    )
    rule_stream: Mapped[OutputStream | None] = mapped_column(
        enum_column(OutputStream, "output_stream"), nullable=True
    )
    rule_location: Mapped[ConditionLocation | None] = mapped_column(
        enum_column(ConditionLocation, "condition_location"), nullable=True
    )
    rule_operator: Mapped[MatchOperator | None] = mapped_column(
        enum_column(MatchOperator, "match_operator"), nullable=True
    )
    rule_anchor: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    rule_line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_case_sensitive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    condition_matched: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # NULL when the location does not use an anchor.
    anchor_found: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evaluated_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Short excerpt of the compared text, for "why did it match?".
    observed: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Set when a condition could not be evaluated safely (e.g. truncated output).
    # The step is then FAILED and this explains why, instead of a silent no-match.
    evaluation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    host_run: Mapped[ExecutionHostRun] = relationship(back_populates="step_runs")


class ExecutionVariableDiscovery(Base):
    """One variable's discovery attempt on one host.

    Records *that* a value was captured and how long it took, never the value
    itself. The discovery *command* is copied here, which is what makes a run
    immune to a variable edited while it is in flight: the runner executes what is
    frozen on the row, not what the workflow says today. ``detail`` holds a
    bounded failure reason or a short preview of the captured text.
    """

    __tablename__ = "execution_variable_discoveries"
    __table_args__ = (
        UniqueConstraint(
            "host_run_id",
            "name",
            name="uq_execution_variable_discoveries_host_run_id_name",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_run_id: Mapped[int] = mapped_column(
        ForeignKey("execution_host_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Frozen at execution creation, so a mid-run edit cannot change the command.
    # Empty for a user-defined variable, whose value is frozen in ``static_value``.
    discovery_command: Mapped[str] = mapped_column(Text, nullable=False, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    # Frozen source, so history explains *why* there is (or is not) a command.
    source: Mapped[VariableSource] = mapped_column(
        enum_column(VariableSource, "variable_source"),
        nullable=False,
        default=VariableSource.COMMAND_OUTPUT,
    )
    # A user-defined value, copied here so an in-flight run is immune to an edit.
    # Never a discovery result: those are recorded as metadata only.
    static_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[StepRunStatus] = mapped_column(
        enum_column(StepRunStatus, "step_run_status"),
        nullable=False,
        default=StepRunStatus.PENDING,
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Short preview for debugging; never the full value.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    host_run: Mapped[ExecutionHostRun] = relationship(back_populates="variable_discoveries")


class ExecutionRunAsTransition(Base):
    """A Run-As context transition attempted on one host.

    One row per block activation, so history shows which identity a run entered,
    whether the transition succeeded, and why it failed -- without the credential.
    """

    __tablename__ = "execution_run_as_transitions"
    __table_args__ = (
        UniqueConstraint(
            "host_run_id",
            "position",
            name="uq_execution_run_as_transitions_host_run_id_position",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_run_id: Mapped[int] = mapped_column(
        ForeignKey("execution_host_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_username: Mapped[str] = mapped_column(String(120), nullable=False)
    method: Mapped[RunAsMethod] = mapped_column(
        enum_column(RunAsMethod, "run_as_method"), nullable=False
    )
    status: Mapped[StepRunStatus] = mapped_column(
        enum_column(StepRunStatus, "step_run_status"),
        nullable=False,
        default=StepRunStatus.PENDING,
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    host_run: Mapped[ExecutionHostRun] = relationship(back_populates="run_as_transitions")
