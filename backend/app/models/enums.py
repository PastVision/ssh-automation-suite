"""Lifecycle states recorded in execution history."""

from __future__ import annotations

from enum import StrEnum


class ExecutionTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HostRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    # Set when a host was still PENDING when the execution was cancelled, i.e. it
    # never started. A host interrupted mid-flight is left FAILED with an
    # explanatory error_message instead, because it did not skip cleanly.
    CANCELLED = "cancelled"
    # A ``TERMINATE_IF`` rule matched: the host stopped early *on purpose* and the
    # remaining steps were not run. This is a success, not a failure -- no error
    # occurred -- so it must never be reported as FAILED. It is counted with the
    # succeeded hosts in ``Execution.succeeded_host_count`` while staying visible
    # as its own state so the UI can explain why the workflow ended early.
    TERMINATED = "terminated"


class StepRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class StepAction(StrEnum):
    """What a step does after its command completes successfully.

    ``CONTINUE`` is the default and the behaviour of every step that predates
    execution rules, so a template with no rules runs exactly as it always did.
    A conditional action is evaluated *only* after a clean exit; it can never
    reinterpret a nonzero exit, a timeout, a transport failure or a cancellation.
    """

    CONTINUE = "continue"
    CONTINUE_IF = "continue_if"
    SKIP_NEXT_IF = "skip_next_if"
    TERMINATE_IF = "terminate_if"
    FAIL_IF = "fail_if"


class OutputStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    COMBINED = "combined"


class ConditionLocation(StrEnum):
    ANYWHERE = "anywhere"
    LINE_NUMBER = "line_number"
    ANCHOR = "anchor"


class MatchOperator(StrEnum):
    """Deliberately tiny: no regex. See ``app/services/conditions.py``."""

    CONTAINS = "contains"
    EQUALS = "equals"


class StepDecision(StrEnum):
    """The decision a step's rule produced, recorded for the history reader.

    ``CONTINUE``/``SKIP_NEXT``/``TERMINATE``/``FAIL`` describe what the engine did
    *after* evaluation, which is not always the configured action: a
    ``CONTINUE_IF`` whose condition did not match decides ``FAIL``.
    """

    CONTINUE = "continue"
    SKIP_NEXT = "skip_next"
    TERMINATE = "terminate"
    FAIL = "fail"
