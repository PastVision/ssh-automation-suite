"""Execution API.

Deliberately minimal: enough surface to start a run, watch it, and cancel it.
Template and workflow CRUD are out of scope for this milestone, so a caller
supplies the steps and hosts inline.

Responses never include private key material. ``CredentialRef`` carries only the
credential id, which is a database identifier rather than a secret.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import ExecutionStatus, HostRunStatus, StepRunStatus


class StepInput(BaseModel):
    """One script to run, in order, on every host."""

    name: str = Field(min_length=1, max_length=120)
    script: str = Field(min_length=1)
    interpreter: str = Field(default="bash", max_length=40)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)

    @field_validator("interpreter")
    @classmethod
    def _interpreter_is_a_bare_name(cls, value: str) -> str:
        """Only allow a simple program name.

        The interpreter is placed into the remote command line, so a value like
        ``bash; rm -rf /`` must not be able to inject a second command.
        """
        cleaned = value.strip()
        if not cleaned or not cleaned.replace("-", "").replace(".", "").isalnum():
            raise ValueError("interpreter must be a single program name, e.g. 'bash'")
        return cleaned


class HostInput(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)


class ExecutionCreate(BaseModel):
    workflow_name: str = Field(default="ad-hoc execution", min_length=1, max_length=120)
    credential_id: int = Field(ge=1)
    hosts: list[HostInput] = Field(min_length=1, max_length=500)
    steps: list[StepInput] = Field(min_length=1, max_length=100)


class StepRunRead(BaseModel):
    position: int
    template_name: str
    status: StepRunStatus
    exit_code: int | None = None
    duration_ms: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class HostRunRead(BaseModel):
    id: int
    hostname: str
    port: int
    credential_name: str | None = None
    status: HostRunStatus
    failed_step_position: int | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_path: str | None = None
    steps: list[StepRunRead] = Field(default_factory=list)


class ExecutionRead(BaseModel):
    id: int
    workflow_name: str
    status: ExecutionStatus
    trigger: str
    host_count: int
    succeeded_host_count: int
    failed_host_count: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    log_path: str | None = None
    error_message: str | None = None
    hosts: list[HostRunRead] = Field(default_factory=list)


class ExecutionAccepted(BaseModel):
    id: int
    status: ExecutionStatus


class ExecutionSummary(BaseModel):
    """One row of execution history.

    Carries no host detail and no transcript: a list of hundreds of runs should not
    embed every step. Opening a run reads ``ExecutionRead`` instead.
    """

    id: int
    workflow_id: int | None = None
    workflow_name: str
    status: ExecutionStatus
    trigger: str
    host_count: int
    succeeded_host_count: int
    failed_host_count: int
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ExecutionListResponse(BaseModel):
    items: list[ExecutionSummary]
    total: int


class HostTranscriptRead(BaseModel):
    """A host run's filesystem transcript.

    The transcript is written by the runner and deliberately excludes variable
    values and Run-As passwords (discovery records lengths and short previews only),
    so it is safe to return to the execution's owner.
    """

    execution_id: int
    host_run_id: int
    hostname: str
    available: bool
    truncated: bool = False
    content: str = ""


class CancelResponse(BaseModel):
    id: int
    status: ExecutionStatus
    cancel_requested: bool
