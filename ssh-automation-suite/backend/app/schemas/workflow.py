"""Workflow request/response models.

The body is a single ordered ``entries`` list so a Run-As block is positioned in
the same sequence as the template steps it governs. That is deliberately *not*
nested: the execution engine, the UI list, and the history all read one flat,
ordered sequence, and "which block does this step belong to?" is answered by
position rather than by walking a tree.

Request models never contain a password field for Run-As in any response type.
``RunAsInput.password`` is write-only by construction: it exists on the input
model and nowhere on a read model, so it cannot be serialised back out.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.workflow import RunAsMethod


class HostInput(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)


class HostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    hostname: str
    port: int | None


class VariableInput(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    discovery_command: str = Field(min_length=1, max_length=8000)
    description: str | None = Field(default=None, max_length=500)
    timeout_seconds: int = Field(default=30, ge=1, le=3600)


class VariableRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    discovery_command: str
    description: str | None
    timeout_seconds: int


class TemplateStepInput(BaseModel):
    kind: Literal["template"] = "template"
    template_id: int


class TemplateStepRead(BaseModel):
    kind: Literal["template"] = "template"
    position: int
    template_id: int
    template_name: str
    template_version: int


class RunAsInput(BaseModel):
    kind: Literal["run_as"] = "run_as"
    target_username: str = Field(min_length=1, max_length=32)
    method: RunAsMethod = RunAsMethod.NOPASSWD
    # Write-only. The service encrypts it immediately; no read model has it.
    password: str | None = Field(default=None, max_length=1024)


class RunAsRead(BaseModel):
    """A Run-As block as returned to a client. Never includes the password."""

    kind: Literal["run_as"] = "run_as"
    position: int
    target_username: str
    method: RunAsMethod
    # Lets the UI say "a password is stored" without ever receiving it.
    has_password: bool


EntryInput = Annotated[TemplateStepInput | RunAsInput, Field(discriminator="kind")]
EntryRead = Annotated[TemplateStepRead | RunAsRead, Field(discriminator="kind")]


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    credential_id: int | None = None
    entries: list[EntryInput] = Field(default_factory=list)
    variables: list[VariableInput] = Field(default_factory=list)
    hosts: list[HostInput] = Field(default_factory=list)


class WorkflowUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    credential_id: int | None = None
    entries: list[EntryInput] = Field(default_factory=list)
    variables: list[VariableInput] = Field(default_factory=list)
    hosts: list[HostInput] = Field(default_factory=list)
    # Optimistic concurrency. Optional so a first save from a stale form still
    # gets the lock check rather than a silent overwrite.
    row_version: int | None = None


class WorkflowSummary(BaseModel):
    id: int
    name: str
    description: str | None
    owner_id: int
    row_version: int
    can_edit: bool
    is_owner: bool
    step_count: int
    run_as_count: int
    variable_count: int
    host_count: int
    created_at: datetime
    updated_at: datetime


class WorkflowDetail(BaseModel):
    id: int
    name: str
    description: str | None
    owner_id: int
    credential_id: int | None
    row_version: int
    can_edit: bool
    is_owner: bool
    entries: list[EntryRead]
    variables: list[VariableRead]
    hosts: list[HostRead]
    created_at: datetime
    updated_at: datetime


class WorkflowListResponse(BaseModel):
    items: list[WorkflowSummary]


class WorkflowRunRequest(BaseModel):
    """Optional narrowing of a run, so the same workflow can be re-run on a subset."""

    hostnames: list[str] | None = None


class WorkflowRunResponse(BaseModel):
    execution_id: int
