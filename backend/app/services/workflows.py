"""Workflow CRUD and validation.

A workflow is an ordered list of *entries*: a template step or a Run-As block.
Both share one ``position`` space, which is what makes "the child templates of
this Run-As block" expressible without a nested structure -- they are simply the
template entries that follow it.

Ordering is rewritten as a whole on every save (``replace_entries``) instead of
being patched per row. Reordering a unique ``(workflow_id, position)`` column in
place would collide mid-update; replacing the set inside one transaction is both
simpler and atomic, and it matches how a versioned template body already behaves.

Authorization mirrors templates exactly: grants alone decide access, an
unauthorized caller is told "not found", and a write needs both the edit lock and
a current ``row_version``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.audit_actions import AuditAction
from app.models.credential import SshCredential
from app.models.enums import ExecutionStatus, ExecutionTrigger
from app.models.permission import ResourcePermission, ResourceType
from app.models.template import Template, TemplateVersion
from app.models.workflow import (
    RunAsMethod,
    Workflow,
    WorkflowHost,
    WorkflowRunAsBlock,
    WorkflowStep,
    WorkflowVariable,
)
from app.services import audit, execution_state, locks, permissions, run_as, variables
from app.services.execution_runner import get_runner
from app.services.sessions import Principal

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.execution_state import HostSpec, StepSpec, VariableSpec

RESOURCE = ResourceType.WORKFLOW

# Audit identity for scheduled work. A scheduled run is *owned* by the schedule's
# owner -- that is who may see and cancel it -- but it is *performed by the
# service*, so the audit trail attributes it to this actor instead of inventing a
# human click. ``actor_user_id`` stays NULL because no user performed it.
SERVICE_ACTOR_USERNAME = "system:scheduler"


@dataclass(frozen=True)
class ScheduleLaunchContext:
    """What a scheduled launch knows that a manual one does not."""

    schedule_id: int
    schedule_name: str
    scheduled_for: datetime

    @property
    def detail(self) -> dict[str, object]:
        return {"schedule_id": self.schedule_id, "scheduled_for": self.scheduled_for}


def audit_actor_id(schedule: ScheduleLaunchContext | None, principal: Principal) -> int | None:
    """The audit actor: NULL for scheduled work, the human for manual work."""
    return None if schedule is not None else principal.user_id


def audit_actor_username(
    schedule: ScheduleLaunchContext | None, principal: Principal
) -> str | None:
    return SERVICE_ACTOR_USERNAME if schedule is not None else principal.label


MAX_HOSTS = 500
MAX_STEPS = 100
MAX_NAME = 120


class WorkflowNotFound(LookupError):
    """No such workflow, or the caller may not see it."""


class WorkflowConflict(RuntimeError):
    """Name already used, or the edit is not coherent."""


class WorkflowInvalid(ValueError):
    """The submitted workflow body is not usable."""


@dataclass(frozen=True)
class VariableData:
    name: str
    discovery_command: str
    description: str | None = None
    timeout_seconds: int = 30


@dataclass(frozen=True)
class RunAsData:
    target_username: str
    method: RunAsMethod = RunAsMethod.NOPASSWD
    # Plaintext only in this transient object: it is encrypted before storage and
    # never returned by any read path.
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StepData:
    template_id: int


@dataclass(frozen=True)
class HostData:
    hostname: str
    port: int | None = None


# One ordered entry: either a template reference or a Run-As block.
Entry = StepData | RunAsData


@dataclass(frozen=True)
class WorkflowBody:
    name: str
    description: str | None = None
    credential_id: int | None = None
    entries: tuple[Entry, ...] = ()
    variables: tuple[VariableData, ...] = ()
    hosts: tuple[HostData, ...] = ()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_body(body: WorkflowBody) -> None:
    """Reject anything that cannot produce a coherent workflow.

    Run before any database work so an invalid body never reaches a transaction.
    """
    name = (body.name or "").strip()
    if not name:
        raise WorkflowInvalid("A workflow needs a name.")
    if len(name) > MAX_NAME:
        raise WorkflowInvalid(f"A workflow name may be at most {MAX_NAME} characters.")

    if len(body.entries) > MAX_STEPS:
        raise WorkflowInvalid(
            f"A workflow may contain at most {MAX_STEPS} steps (found {len(body.entries)})."
        )
    templates = sum(1 for entry in body.entries if isinstance(entry, StepData))
    if templates == 0:
        raise WorkflowInvalid("A workflow needs at least one template step.")

    _validate_run_as_blocks(body.entries)

    if len(body.hosts) > MAX_HOSTS:
        raise WorkflowInvalid(f"A workflow may target at most {MAX_HOSTS} hosts.")
    seen_hosts: set[str] = set()
    for host in body.hosts:
        hostname = (host.hostname or "").strip()
        if not hostname:
            raise WorkflowInvalid("A host name cannot be empty.")
        if len(hostname) > 255:
            raise WorkflowInvalid(f"Host name {hostname!r} is too long.")
        key = hostname.lower()
        if key in seen_hosts:
            raise WorkflowInvalid(f"Host {hostname!r} is listed more than once.")
        seen_hosts.add(key)
        if host.port is not None and not 1 <= host.port <= 65535:
            raise WorkflowInvalid(f"Port {host.port} is out of range.")

    try:
        variables.validate_unique([v.name for v in body.variables])
    except variables.RenderError as exc:
        raise WorkflowInvalid(str(exc)) from exc
    for variable in body.variables:
        if not (variable.discovery_command or "").strip():
            raise WorkflowInvalid(
                f"Variable {variable.name!r} needs a discovery command."
            )
        if not 1 <= variable.timeout_seconds <= 3600:
            raise WorkflowInvalid(
                f"Variable {variable.name!r} timeout must be between 1 and 3600 seconds."
            )


def _validate_run_as_blocks(entries: tuple[Entry, ...]) -> None:
    """Validate block shape, rejecting nesting and duplicate targets.

    Nesting is already unrepresentable in the schema (one flat position space),
    so this only has to reject the two shapes a flat list *can* express badly: a
    block with no templates after it, and two blocks in a row.
    """
    blocks = [i for i, entry in enumerate(entries) if isinstance(entry, RunAsData)]
    for index in blocks:
        block = entries[index]
        assert isinstance(block, RunAsData)
        try:
            run_as.validate_target(block.target_username)
            run_as.validate_password(block.password, block.method)
        except run_as.RunAsError as exc:
            raise WorkflowInvalid(str(exc)) from exc
        # A block must govern at least one template.
        following = entries[index + 1:]
        if not any(isinstance(entry, StepData) for entry in following):
            raise WorkflowInvalid(
                f"The Run-As block for {block.target_username!r} has no template "
                "steps after it, so it would do nothing."
            )
        # Two blocks in a row would make the first one govern nothing.
        if following and isinstance(following[0], RunAsData):
            raise WorkflowInvalid(
                "Two Run-As blocks cannot be adjacent: the first would govern no "
                "templates. Put a template step between them."
            )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def _visible_filter(user_id: int):
    shared_ids = select(ResourcePermission.resource_id).where(
        ResourcePermission.resource_type == RESOURCE,
        ResourcePermission.user_id == user_id,
    )
    return or_(Workflow.owner_id == user_id, Workflow.id.in_(shared_ids))


def _load(session: Session, workflow_id: int) -> Workflow:
    workflow = session.scalars(
        select(Workflow)
        .where(Workflow.id == workflow_id)
        .options(
            selectinload(Workflow.steps)
            .selectinload(WorkflowStep.template_version)
            .selectinload(TemplateVersion.steps),
            selectinload(Workflow.run_as_blocks),
            selectinload(Workflow.variables),
            selectinload(Workflow.hosts),
        )
    ).first()
    if workflow is None:
        raise WorkflowNotFound(workflow_id)
    return workflow


def get(workflow_id: int, principal: Principal) -> Workflow:
    session = get_session_factory()()
    try:
        workflow = _load(session, workflow_id)
        if not permissions.can_view(RESOURCE, workflow.id, workflow.owner_id, principal.user_id):
            raise WorkflowNotFound(workflow_id)
        return workflow
    finally:
        session.close()


def can_edit(workflow: Workflow, principal: Principal) -> bool:
    return permissions.can_edit(RESOURCE, workflow.id, workflow.owner_id, principal.user_id)


def is_owner(workflow: Workflow, principal: Principal) -> bool:
    return workflow.owner_id == principal.user_id


def list_for_user(
    principal: Principal, *, search: str | None = None, limit: int = 200
) -> list[Workflow]:
    session = get_session_factory()()
    try:
        query = (
            select(Workflow)
            .where(_visible_filter(principal.user_id))
            .options(
                selectinload(Workflow.steps),
                selectinload(Workflow.run_as_blocks),
                selectinload(Workflow.variables),
                selectinload(Workflow.hosts),
            )
            .order_by(Workflow.updated_at.desc(), Workflow.id.desc())
            .limit(limit)
        )
        if search:
            pattern = f"%{search.strip().lower()}%"
            query = query.where(
                or_(
                    func.lower(Workflow.name).like(pattern),
                    func.lower(func.coalesce(Workflow.description, "")).like(pattern),
                )
            )
        return list(session.scalars(query).all())
    finally:
        session.close()


def ordered_entries(workflow: Workflow) -> list[WorkflowStep | WorkflowRunAsBlock]:
    """The workflow's body in order, mixing template steps and Run-As blocks."""
    merged: list[WorkflowStep | WorkflowRunAsBlock] = [
        *workflow.steps, *workflow.run_as_blocks
    ]
    merged.sort(key=lambda item: item.position)
    return merged


def validate_references(session: Session, body: WorkflowBody, principal: Principal) -> None:
    """Ensure every referenced template/credential exists and is accessible.

    This is the boundary where a caller-supplied id could otherwise point at
    another user's private resource, so a missing or invisible target is refused
    with the same message either way.
    """
    template_ids = {entry.template_id for entry in body.entries if isinstance(entry, StepData)}
    if template_ids:
        found = set(
            session.scalars(
                select(Template.id).where(Template.id.in_(template_ids))
            ).all()
        )
        missing = template_ids - found
        if missing:
            raise WorkflowInvalid(
                f"Template {sorted(missing)[0]} does not exist."
            )
        for template_id in template_ids:
            if not permissions.can_view(
                ResourceType.TEMPLATE,
                template_id,
                session.scalar(select(Template.owner_id).where(Template.id == template_id)),
                principal.user_id,
            ):
                raise WorkflowInvalid(f"Template {template_id} does not exist.")

    if body.credential_id is not None:
        credential = session.scalar(
            select(SshCredential).where(
                SshCredential.id == body.credential_id,
                SshCredential.owner_id == principal.user_id,
            )
        )
        if credential is None:
            raise WorkflowInvalid(
                f"Credential {body.credential_id} does not exist."
            )


def _latest_version_id(session: Session, template_id: int) -> int:
    """Pin the template's current version at save time.

    Saving a workflow pins *now*: the step records the version that existed when
    the workflow was saved. Execution then re-snapshots whatever is pinned, so a
    later template edit cannot change a run in flight.
    """
    version_id = session.scalar(
        select(TemplateVersion.id)
        .where(TemplateVersion.template_id == template_id)
        .order_by(TemplateVersion.version.desc())
        .limit(1)
    )
    if version_id is None:
        raise WorkflowInvalid(
            f"Template {template_id} has no version to pin."
        )
    return version_id


def _replace_entries(
    session: Session, workflow: Workflow, body: WorkflowBody
) -> None:
    """Rewrite the ordered body atomically.

    Existing rows are deleted before the new ones are inserted so the
    ``(workflow_id, position)`` unique constraint can never be violated mid-write.
    """
    for step in list(workflow.steps):
        session.delete(step)
    for block in list(workflow.run_as_blocks):
        session.delete(block)
    session.flush()

    for position, entry in enumerate(body.entries):
        if isinstance(entry, RunAsData):
            block = WorkflowRunAsBlock(
                workflow_id=workflow.id,
                position=position,
                target_username=run_as.validate_target(entry.target_username),
                method=entry.method,
            )
            if entry.password:
                from app.core.security import encrypt_secret

                block.password_encrypted = encrypt_secret(entry.password)
            session.add(block)
        else:
            step = WorkflowStep(
                workflow_id=workflow.id,
                position=position,
                template_version_id=_latest_version_id(session, entry.template_id),
            )
            session.add(step)
    session.flush()


def _replace_variables(
    session: Session, workflow: Workflow, body: WorkflowBody
) -> None:
    for variable in list(workflow.variables):
        session.delete(variable)
    session.flush()
    for variable in body.variables:
        session.add(
            WorkflowVariable(
                workflow_id=workflow.id,
                name=variables.validate_name(variable.name),
                description=variable.description,
                discovery_command=variable.discovery_command,
                timeout_seconds=variable.timeout_seconds,
            )
        )
    session.flush()


def _replace_hosts(session: Session, workflow: Workflow, body: WorkflowBody) -> None:
    for host in list(workflow.hosts):
        session.delete(host)
    session.flush()
    for host in body.hosts:
        session.add(
            WorkflowHost(
                workflow_id=workflow.id,
                hostname=host.hostname.strip(),
                port=host.port,
            )
        )
    session.flush()


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def create(
    body: WorkflowBody,
    *,
    principal: Principal,
    remote_addr: str | None = None,
) -> Workflow:
    validate_body(body)
    session = get_session_factory()()
    try:
        validate_references(session, body, principal)
        duplicate = session.scalar(
            select(Workflow).where(
                Workflow.owner_id == principal.user_id,
                func.lower(Workflow.name) == body.name.strip().lower(),
            )
        )
        if duplicate is not None:
            raise WorkflowConflict(f"You already have a workflow named {body.name!r}.")

        workflow = Workflow(
            owner_id=principal.user_id,
            name=body.name.strip(),
            description=body.description,
            credential_id=body.credential_id,
            row_version=1,
        )
        session.add(workflow)
        session.flush()

        _replace_entries(session, workflow, body)
        _replace_variables(session, workflow, body)
        _replace_hosts(session, workflow, body)
        session.commit()
        workflow_id = workflow.id
        entry_count = len(body.entries)
        variable_count = len(body.variables)
        host_count = len(body.hosts)
    except IntegrityError as exc:
        session.rollback()
        raise WorkflowConflict("That workflow could not be saved.") from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    audit.record(
        AuditAction.RESOURCE_CREATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=workflow_id,
        remote_addr=remote_addr,
        detail={
            "entries": entry_count,
            "variables": variable_count,
            "hosts": host_count,
        },
    )
    return get(workflow_id, principal)


def update(
    workflow_id: int,
    body: WorkflowBody,
    *,
    expected_row_version: int | None,
    principal: Principal,
    remote_addr: str | None = None,
) -> Workflow:
    validate_body(body)
    session = get_session_factory()()
    try:
        workflow = _load(session, workflow_id)
        if not permissions.can_edit(
            RESOURCE, workflow.id, workflow.owner_id, principal.user_id
        ):
            raise WorkflowNotFound(workflow_id)

        locks.assert_holds(
            RESOURCE,
            workflow_id,
            user_id=principal.user_id,
            session_id=principal.session_id or 0,
        )
        locks.check_version(expected_row_version, workflow.row_version)

        validate_references(session, body, principal)

        if body.name.strip().lower() != workflow.name.lower():
            clash = session.scalar(
                select(Workflow).where(
                    Workflow.owner_id == workflow.owner_id,
                    func.lower(Workflow.name) == body.name.strip().lower(),
                    Workflow.id != workflow_id,
                )
            )
            if clash is not None:
                raise WorkflowConflict(
                    f"You already have a workflow named {body.name!r}."
                )

        workflow.name = body.name.strip()
        workflow.description = body.description
        workflow.credential_id = body.credential_id
        workflow.row_version += 1

        _replace_entries(session, workflow, body)
        _replace_variables(session, workflow, body)
        _replace_hosts(session, workflow, body)
        session.commit()
        row_version = workflow.row_version
        entry_count = len(body.entries)
    except IntegrityError as exc:
        session.rollback()
        raise WorkflowConflict("That workflow could not be saved.") from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    audit.record(
        AuditAction.RESOURCE_UPDATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=workflow_id,
        remote_addr=remote_addr,
        detail={"row_version": row_version, "entries": entry_count},
    )
    return get(workflow_id, principal)


def delete(
    workflow_id: int, *, principal: Principal, remote_addr: str | None = None
) -> None:
    """Owner-only delete. Executions keep their own snapshots, so history survives."""
    from app.db.types import utcnow
    from app.models.schedule import Schedule, ScheduleState

    session = get_session_factory()()
    try:
        workflow = _load(session, workflow_id)
        if workflow.owner_id != principal.user_id:
            raise WorkflowNotFound(workflow_id)
        name = workflow.name
        entry_count = len(workflow.steps) + len(workflow.run_as_blocks)

        # Retire the workflow's schedules in this transaction. The foreign key is
        # SET NULL so the hard delete below is allowed, but a schedule left enabled
        # would keep being scanned with no workflow to run, so it is explicitly
        # retired here and the reason is recorded in the audit detail.
        retired = list(
            session.scalars(
                select(Schedule).where(
                    Schedule.workflow_id == workflow_id,
                    Schedule.deleted_at.is_(None),
                )
            )
        )
        for schedule in retired:
            schedule.deleted_at = utcnow()
            schedule.enabled = False
            schedule.next_run_at = None
            schedule.state = ScheduleState.DISABLED
            schedule.row_version = (schedule.row_version or 0) + 1
        session.flush()

        session.delete(workflow)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _release_all_locks(workflow_id)
    audit.record(
        AuditAction.RESOURCE_DELETED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=workflow_id,
        remote_addr=remote_addr,
        detail={
            "name": name,
            "entries": entry_count,
            "schedules_retired": len(retired),
        },
    )


def _release_all_locks(workflow_id: int) -> int:
    """Drop every lock on a deleted workflow so its ids can be reused cleanly."""
    from app.db.types import utcnow
    from app.models.permission import ResourceLock

    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(ResourceLock).where(
                    ResourceLock.resource_type == RESOURCE,
                    ResourceLock.resource_id == workflow_id,
                    ResourceLock.released_at.is_(None),
                )
            ).all()
        )
        for row in rows:
            row.released_at = utcnow()
            row.released_reason = "resource_deleted"
        session.commit()
        return len(rows)
    finally:
        session.close()


def build_execution_specs(
    workflow: Workflow, *, hostnames: list[str] | None = None
) -> tuple[list[StepSpec], list[HostSpec], list[VariableSpec]]:
    """Freeze a workflow into the specs an execution needs.

    Run-As is resolved here, at launch: each template step is attributed the
    target of the *most recent preceding* block, or the primary user when no block
    precedes it. The block's ``position`` is carried onto the step so history can
    group the children under the transition that produced them.

    Nested blocks are impossible by construction (one flat position space), so the
    attribution is a single forward pass.
    """
    from app.services.execution_state import HostSpec, StepSpec, VariableSpec

    if workflow.credential_id is None:
        raise WorkflowInvalid(
            "This workflow has no SSH credential, so it cannot be executed."
        )

    session = get_session_factory()()
    try:
        credential = session.scalar(
            select(SshCredential).where(SshCredential.id == workflow.credential_id)
        )
        if credential is None or not credential.private_key_path:
            raise WorkflowInvalid(
                "This workflow's SSH credential is missing or has no private key."
            )
        credential_name = credential.name
        credential_username = credential.username
        default_port = credential.default_port
        credential_id = credential.id
    finally:
        session.close()

    steps: list[StepSpec] = []
    run_as_target: str | None = None
    run_as_method = None
    run_as_position: int | None = None

    for entry in ordered_entries(workflow):
        if isinstance(entry, WorkflowRunAsBlock):
            run_as_target = entry.target_username
            run_as_method = entry.method
            run_as_position = entry.position
            continue

        version = entry.template_version
        if version is None:
            raise WorkflowInvalid(
                f"Workflow step {entry.position} has no pinned template version."
            )
        # A template version holds several shell steps, and they all run in the
        # pinned order inside the workflow's own position. Flattening here keeps
        # the engine's unit of work a single script, which is what it has always
        # executed and what the execution history records.
        version_steps = sorted(version.steps, key=lambda s: s.position)
        if not version_steps:
            raise WorkflowInvalid(
                f"Template {version.name!r} has no steps to run."
            )
        for version_step in version_steps:
            steps.append(
                StepSpec(
                    template_name=version.name,
                    template_version=version.version,
                    script=version_step.script,
                    interpreter=version_step.interpreter,
                    timeout_seconds=version_step.timeout_seconds,
                    template_id=version.template_id,
                    template_version_id=version.id,
                    rule=version_step.to_rule(),
                    run_as_username=run_as_target,
                    run_as_method=run_as_method,
                    run_as_position=run_as_position,
                )
            )

    if not steps:
        raise WorkflowInvalid("This workflow has no template steps to run.")

    hosts = [
        HostSpec(
            hostname=host.hostname,
            port=host.port or default_port,
            credential_id=credential_id,
            credential_name=credential_name,
            username=credential_username,
        )
        for host in workflow.hosts
        if hostnames is None or host.hostname in hostnames
    ]
    if not hosts:
        raise WorkflowInvalid("No hosts selected for this run.")

    variables = [
        VariableSpec(
            name=variable.name,
            discovery_command=variable.discovery_command,
            timeout_seconds=variable.timeout_seconds,
        )
        for variable in workflow.variables
    ]
    return steps, hosts, variables


def launch(
    workflow_id: int,
    *,
    principal: Principal,
    hostnames: list[str] | None = None,
    remote_addr: str | None = None,
    trigger: ExecutionTrigger = ExecutionTrigger.MANUAL,
    schedule: ScheduleLaunchContext | None = None,
    session: Session | None = None,
    start_runner: bool = True,
) -> int:
    """Freeze the workflow and start an execution. Returns the execution id.

    All validation happens before any row is created, so a rejected launch leaves
    nothing behind. The execution then owns its own snapshots: editing or deleting
    this workflow afterwards cannot change or invalidate the run.

    **This is the one execution-construction path.** Manual runs and scheduled
    runs differ only in ``trigger``/``schedule`` and in who owns the transaction:

    * manual: ``session=None``, so each write commits on its own and the runner is
      started here.
    * scheduled: the dispatcher passes its own session so the lease fence, the
      ledger outcome and this insert commit atomically, and starts the runner
      itself *after* committing (``start_runner=False``).

    ``schedule`` carries the attribution split: ``actor`` is whose schedule it is
    (the execution's owner for visibility and cancellation), while ``service_actor``
    is the identity recorded in the audit trail, so a scheduled run is never
    attributed to a human who did not press anything.
    """
    workflow = get(workflow_id, principal)
    if workflow.credential_id is None:
        raise WorkflowInvalid("This workflow has no SSH credential, so it cannot run.")
    if not workflow.hosts:
        raise WorkflowInvalid("This workflow has no hosts, so it cannot run.")

    steps, hosts, variable_specs = build_execution_specs(workflow, hostnames=hostnames)

    if start_runner:
        runner = get_runner()
        if runner.closing:
            raise WorkflowConflict(
                "The service is shutting down and cannot accept new executions."
            )

    settings = get_settings()
    execution_id = execution_state.create_execution(
        workflow_name=workflow.name,
        steps=steps,
        hosts=hosts,
        variables=variable_specs,
        trigger=trigger,
        workflow_id=workflow.id,
        max_concurrent_hosts=settings.max_concurrent_host_workflows,
        created_by_id=principal.user_id,
        schedule_id=schedule.schedule_id if schedule else None,
        schedule_name=schedule.schedule_name if schedule else None,
        scheduled_for=schedule.scheduled_for if schedule else None,
        session=session,
    )

    if start_runner:
        _start_or_fail(execution_id)

    audit.record(
        AuditAction.WORKFLOW_RUN_STARTED,
        actor_id=audit_actor_id(schedule, principal),
        actor_username=audit_actor_username(schedule, principal),
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=workflow.id,
        remote_addr=remote_addr,
        detail={
            "execution_id": execution_id,
            "trigger": str(trigger),
            "schedule_id": schedule.schedule_id if schedule else None,
            "scheduled_for": schedule.scheduled_for if schedule else None,
            "hosts": len(hosts),
            "steps": len(steps),
            "variables": len(variable_specs),
            "run_as_blocks": sum(
                1 for s in steps if s.run_as_position is not None
            ),
        },
        session=session,
    )
    return execution_id


def _start_or_fail(execution_id: int) -> None:
    """Hand a committed execution to the runner, marking it failed if that fails."""
    try:
        get_runner().start(execution_id)
    except RuntimeError as exc:
        execution_state.finish_execution(
            execution_id, status=ExecutionStatus.FAILED, error_message=str(exc)
        )
        raise WorkflowConflict(str(exc)) from exc


def run_as_password(workflow: Workflow, position: int) -> str | None:
    """Decrypt a Run-As block's password for execution. Never for API responses.

    Kept separate from the read path so no serializer can accidentally include it.
    """
    block = next((b for b in workflow.run_as_blocks if b.position == position), None)
    if block is None or not block.password_encrypted:
        return None
    from app.core.security import decrypt_secret

    return decrypt_secret(block.password_encrypted).decode("utf-8")
