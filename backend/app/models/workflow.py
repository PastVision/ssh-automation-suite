"""Workflows: an ordered list of template steps run against a set of hosts.

Steps pin an explicit ``template_version_id`` so a workflow edit does not
silently change what already-scheduled runs execute.

A workflow's ordered body is a single ``position`` space holding two kinds of
entry: a template ``WorkflowStep`` and a ``WorkflowRunAsBlock``. A template runs
under the block that most recently preceded it, which is why a block carries no
step list of its own -- "the child templates" are simply the following template
steps. That representation makes *nested* Run-As unrepresentable rather than
merely rejected by validation.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import enum_column, timestamp_column
from app.models.enums import ExecutionTrigger, RunAsMethod, VariableSource
from app.models.template import TemplateVersion

__all__ = [
    "ExecutionPermission",
    "RunAsMethod",
    "Workflow",
    "WorkflowHost",
    "WorkflowRunAsBlock",
    "WorkflowStep",
    "WorkflowVariable",
    "WorkflowVisibility",
]


class WorkflowVisibility(StrEnum):
    """Who may see the workflow definition itself.

    Mirrors ``TemplateVisibility``: ``PUBLIC`` widens *view* access to any
    authenticated user, and never grants edit or execution rights.
    """

    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"


class ExecutionPermission(StrEnum):
    """Who may start a run of this workflow.

    Kept separate from visibility on purpose: publishing a workflow *definition*
    must never let every user start SSH work against its hosts. A user who can
    view but not execute still sees the definition and its history (subject to
    log visibility), but ``POST /run`` is refused.
    """

    OWNER_ONLY = "owner_only"
    SELECTED = "selected"
    ALL_AUTHENTICATED = "all_authenticated"


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_workflows_owner_id_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optimistic concurrency token, same contract as ``Template.row_version``.
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("ssh_credentials.id", ondelete="RESTRICT"), nullable=True
    )

    # Who may see this definition. Default PRIVATE so every existing workflow
    # keeps exactly the isolation it had before visibility existed.
    visibility: Mapped[WorkflowVisibility] = mapped_column(
        enum_column(WorkflowVisibility, "workflow_visibility"),
        nullable=False,
        default=WorkflowVisibility.PRIVATE,
    )
    # Who may *start* it. Deliberately independent of ``visibility``: a public
    # definition does not imply permission to run SSH work against its hosts.
    execution_permission: Mapped[ExecutionPermission] = mapped_column(
        enum_column(ExecutionPermission, "execution_permission"),
        nullable=False,
        default=ExecutionPermission.OWNER_ONLY,
    )

    # Scheduler wiring is stored now so enabling it later needs no migration.
    schedule_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    schedule_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    trigger_type: Mapped[ExecutionTrigger] = mapped_column(
        enum_column(ExecutionTrigger, "execution_trigger"),
        nullable=False,
        default=ExecutionTrigger.MANUAL,
    )

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)

    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowStep.position",
    )
    run_as_blocks: Mapped[list[WorkflowRunAsBlock]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowRunAsBlock.position",
    )
    variables: Mapped[list[WorkflowVariable]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowVariable.name",
    )
    hosts: Mapped[list[WorkflowHost]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Workflow id={self.id} name={self.name!r}>"


class WorkflowStep(Base):
    """One template in the ordered sequence executed on every host."""

    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("workflow_id", "position", name="uq_workflow_steps_workflow_id_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_version_id: Mapped[int] = mapped_column(
        ForeignKey("template_versions.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    workflow: Mapped[Workflow] = relationship(back_populates="steps")
    template_version: Mapped[TemplateVersion] = relationship()


class WorkflowRunAsBlock(Base):
    """A Run-As context switch occupying a slot in the workflow's step order.

    The child templates are the ``WorkflowStep`` rows that follow this block and
    precede the next block, so nothing here needs a child list. Credentials are
    referenced, never copied: only a credential *id* and the non-secret target
    username are stored.
    """

    __tablename__ = "workflow_run_as_blocks"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "position", name="uq_workflow_run_as_blocks_workflow_id_position"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    target_username: Mapped[str] = mapped_column(String(120), nullable=False)
    method: Mapped[RunAsMethod] = mapped_column(
        enum_column(RunAsMethod, "run_as_method"),
        nullable=False,
        default=RunAsMethod.NOPASSWD,
    )
    # The Run-As target's own password for ``sudo -S``. Distinct from the primary
    # user's sudo password on ``user_ssh_secrets``.
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    workflow: Mapped[Workflow] = relationship(back_populates="run_as_blocks")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<WorkflowRunAsBlock workflow_id={self.workflow_id} "
            f"pos={self.position} user={self.target_username!r}>"
        )


class WorkflowVariable(Base):
    """A workflow-level variable, either discovered per host or user-defined.

    ``source`` is explicit rather than inferred from an empty command: a
    user-defined variable legitimately has no discovery command, and a
    command-backed one whose command happens to be blank must stay invalid. The
    stored value for ``USER_DEFINED`` is *not* secret in the same sense as a
    credential, but it is still never copied into execution history -- it is
    resolved onto the execution's frozen discovery rows like any other.
    """

    __tablename__ = "workflow_variables"
    __table_args__ = (
        UniqueConstraint("workflow_id", "name", name="uq_workflow_variables_workflow_id_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[VariableSource] = mapped_column(
        enum_column(VariableSource, "variable_source"),
        nullable=False,
        default=VariableSource.COMMAND_OUTPUT,
    )
    # Required for COMMAND_OUTPUT, must be empty for USER_DEFINED. Kept NOT NULL
    # with a '' default so the existing column needs no rebuild and every legacy
    # row stays valid.
    discovery_command: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Only meaningful for USER_DEFINED. Not encrypted: it is workflow text the
    # owner typed, equivalent to the script body next to it.
    static_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    workflow: Mapped[Workflow] = relationship(back_populates="variables")


class WorkflowHost(Base):
    """A hostname a workflow targets."""

    __tablename__ = "workflow_hosts"
    __table_args__ = (
        UniqueConstraint("workflow_id", "hostname", name="uq_workflow_hosts_workflow_id_hostname"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)

    workflow: Mapped[Workflow] = relationship(back_populates="hosts")
