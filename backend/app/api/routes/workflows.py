"""Workflow endpoints.

Authorization lives in ``app.services.workflows``; this module only translates
between HTTP and that service, so "can this user do that?" is decided in exactly
one place.

Conventions mirrored from the template routes, for a consistent client:

* A workflow the caller cannot see answers **404**, never 403.
* A write without the edit lock answers **423**; a stale ``row_version`` answers
  **409**.
* A body that cannot form a coherent workflow answers **422**.

The Run-As password is accepted on input and never returned. ``RunAsRead`` simply
has no password field, so no serializer can leak it.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.db.types import as_utc
from app.models.audit_actions import AuditAction, AuditOutcome
from app.models.workflow import Workflow, WorkflowRunAsBlock
from app.schemas.workflow import (
    EntryRead,
    HostRead,
    RunAsInput,
    RunAsRead,
    TemplateStepInput,
    TemplateStepRead,
    VariableRead,
    WorkflowCreate,
    WorkflowDetail,
    WorkflowListResponse,
    WorkflowRunRequest,
    WorkflowRunResponse,
    WorkflowSummary,
    WorkflowUpdate,
)
from app.services import execution_state, locks, permissions, sessions, workflows
from app.services.identity import client_ip, require_principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #


def _entries_from(payload: list) -> tuple[workflows.Entry, ...]:
    entries: list[workflows.Entry] = []
    for item in payload:
        if isinstance(item, RunAsInput):
            entries.append(
                workflows.RunAsData(
                    target_username=item.target_username,
                    method=item.method,
                    password=item.password,
                )
            )
        elif isinstance(item, TemplateStepInput):
            entries.append(workflows.StepData(template_id=item.template_id))
        else:  # pragma: no cover - schema discriminator prevents this
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Unknown workflow entry kind.",
            )
    return tuple(entries)


def _variables_from(payload: list) -> tuple[workflows.VariableData, ...]:
    return tuple(
        workflows.VariableData(
            name=item.name,
            discovery_command=item.discovery_command,
            description=item.description,
            timeout_seconds=item.timeout_seconds,
            # Without these two the client's choice is discarded and every variable
            # silently becomes command-backed, so a static value never survives the
            # round trip from the editor to the execution spec.
            source=item.source,
            static_value=item.static_value,
        )
        for item in payload
    )


def _hosts_from(payload: list) -> tuple[workflows.HostData, ...]:
    return tuple(workflows.HostData(hostname=item.hostname, port=item.port) for item in payload)


def _body(payload: WorkflowCreate | WorkflowUpdate) -> workflows.WorkflowBody:
    return workflows.WorkflowBody(
        name=payload.name,
        description=payload.description,
        credential_id=payload.credential_id,
        entries=_entries_from(payload.entries),
        variables=_variables_from(payload.variables),
        hosts=_hosts_from(payload.hosts),
        # ``None`` on update means "leave unchanged", so it is passed through as
        # ``None`` rather than replaced by a default here. For create the schema
        # already supplies the secure defaults (private, owner-only).
        visibility=payload.visibility,
        execution_permission=payload.execution_permission,
    )


def _entries_read(workflow: Workflow) -> list[EntryRead]:
    entries: list[EntryRead] = []
    for entry in workflows.ordered_entries(workflow):
        if isinstance(entry, WorkflowRunAsBlock):
            entries.append(
                RunAsRead(
                    position=entry.position,
                    target_username=entry.target_username,
                    method=entry.method,
                    has_password=bool(entry.password_encrypted),
                )
            )
        else:
            version = entry.template_version
            entries.append(
                TemplateStepRead(
                    position=entry.position,
                    template_id=version.template_id if version else 0,
                    template_name=version.name if version else "(missing)",
                    template_version=version.version if version else 0,
                )
            )
    return entries


def _detail(workflow: Workflow, principal: sessions.Principal) -> WorkflowDetail:
    return WorkflowDetail(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        owner_id=workflow.owner_id,
        credential_id=workflow.credential_id,
        row_version=workflow.row_version,
        can_edit=workflows.can_edit(workflow, principal),
        can_run=workflows.can_run(workflow, principal),
        is_owner=workflows.is_owner(workflow, principal),
        visibility=workflow.visibility,
        execution_permission=workflow.execution_permission,
        entries=_entries_read(workflow),
        variables=[VariableRead.model_validate(v) for v in workflow.variables],
        hosts=[HostRead.model_validate(h) for h in workflow.hosts],
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


def _summary(
    workflow: Workflow,
    principal: sessions.Principal,
    *,
    active_execution_count: int = 0,
    last_run_at: datetime | None = None,
) -> WorkflowSummary:
    return WorkflowSummary(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        owner_id=workflow.owner_id,
        row_version=workflow.row_version,
        can_edit=workflows.can_edit(workflow, principal),
        can_run=workflows.can_run(workflow, principal),
        is_owner=workflows.is_owner(workflow, principal),
        visibility=workflow.visibility,
        execution_permission=workflow.execution_permission,
        active_execution_count=active_execution_count,
        last_run_at=last_run_at,
        step_count=len(workflow.steps),
        run_as_count=len(workflow.run_as_blocks),
        variable_count=len(workflow.variables),
        host_count=len(workflow.hosts),
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")


def _invalid(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
    )


def _run_as_invalid(exc: workflows.WorkflowRunAsInvalid) -> HTTPException:
    """422 with the offending step, so the editor can mark that row.

    ``detail`` is an object rather than a bare string; clients that only read
    ``detail.message`` keep working, and it carries no credential value -- just the
    target account, which the user typed.
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "message": str(exc),
            "code": "run_as_credential_unavailable",
            "entry_position": exc.entry_position,
            "target_username": exc.target_username,
            "field": exc.field,
        },
    )


def _denied(
    principal: sessions.Principal, request: Request, workflow_id: int, reason: str
) -> None:
    from app.services import audit

    audit.record(
        AuditAction.ACCESS_DENIED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        outcome=AuditOutcome.DENIED,
        resource_type="workflow",
        resource_id=workflow_id,
        remote_addr=client_ip(request),
        detail={"reason": reason},
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("", response_model=WorkflowListResponse, summary="List workflows I can see")
def list_workflows(
    search: str | None = Query(default=None, max_length=200),
    principal: sessions.Principal = Depends(require_principal),
) -> WorkflowListResponse:
    items = workflows.list_for_user(principal, search=search)
    # Two grouped queries for the whole page -- running counts and last-run times --
    # so neither badge can turn a list render into N+1 round trips.
    active_counts = execution_state.active_counts_by_workflow()
    last_runs = execution_state.last_started_at_by_workflow()
    return WorkflowListResponse(
        items=[
            _summary(
                workflow,
                principal,
                active_execution_count=active_counts.get(workflow.id, 0),
                # Normalised because SQLite returns these naive; the schema promises
                # an instant, and an offset-less value would be read as local time by
                # a browser.
                last_run_at=(
                    as_utc(last_runs[workflow.id])
                    if workflow.id in last_runs
                    else None
                ),
            )
            for workflow in items
        ]
    )


@router.post(
    "",
    response_model=WorkflowDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow",
)
def create_workflow(
    payload: WorkflowCreate,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> WorkflowDetail:
    try:
        workflow = workflows.create(
            _body(payload), principal=principal, remote_addr=client_ip(request)
        )
    except workflows.WorkflowConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except workflows.WorkflowRunAsInvalid as exc:
        raise _run_as_invalid(exc) from exc
    except (workflows.WorkflowInvalid, ValueError) as exc:
        raise _invalid(exc) from exc
    return _detail(workflow, principal)


@router.get("/{workflow_id}", response_model=WorkflowDetail, summary="Read a workflow")
def read_workflow(
    workflow_id: int,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> WorkflowDetail:
    try:
        workflow = workflows.get(workflow_id, principal)
    except workflows.WorkflowNotFound as exc:
        _denied(principal, request, workflow_id, "workflow_not_visible")
        raise _not_found() from exc
    return _detail(workflow, principal)


@router.put("/{workflow_id}", response_model=WorkflowDetail, summary="Replace a workflow")
def update_workflow(
    workflow_id: int,
    payload: WorkflowUpdate,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> WorkflowDetail:
    try:
        workflow = workflows.update(
            workflow_id,
            _body(payload),
            expected_row_version=payload.row_version,
            principal=principal,
            remote_addr=client_ip(request),
        )
    except workflows.WorkflowNotFound as exc:
        _denied(principal, request, workflow_id, "workflow_not_editable")
        raise _not_found() from exc
    except locks.LockRequired as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Start editing this workflow before saving: {exc}",
        ) from exc
    except locks.LockConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except workflows.WorkflowConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except workflows.WorkflowRunAsInvalid as exc:
        raise _run_as_invalid(exc) from exc
    except (workflows.WorkflowInvalid, ValueError) as exc:
        raise _invalid(exc) from exc
    return _detail(workflow, principal)


@router.delete(
    "/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a workflow",
)
def delete_workflow(
    workflow_id: int,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> None:
    try:
        workflows.delete(workflow_id, principal=principal, remote_addr=client_ip(request))
    except workflows.WorkflowNotFound as exc:
        _denied(principal, request, workflow_id, "workflow_not_deletable")
        raise _not_found() from exc


@router.post(
    "/{workflow_id}/run",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run a workflow",
)
async def run_workflow(
    workflow_id: int,
    payload: WorkflowRunRequest,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> WorkflowRunResponse:
    try:
        execution_id = workflows.launch(
            workflow_id,
            principal=principal,
            hostnames=payload.hostnames,
            remote_addr=client_ip(request),
        )
    except workflows.WorkflowNotFound as exc:
        _denied(principal, request, workflow_id, "workflow_not_runnable")
        raise _not_found() from exc
    except permissions.NotAuthorized as exc:
        # Visible but not runnable. Recorded as its own reason so the audit trail
        # distinguishes "started an SSH run they were not allowed to" from a plain
        # read denial. ``launch`` raises this before creating anything, so no
        # execution row and no SSH connection exist when this is returned.
        _denied(principal, request, workflow_id, "workflow_not_runnable")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail
        ) from exc
    except workflows.WorkflowConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except workflows.WorkflowRunAsInvalid as exc:
        raise _run_as_invalid(exc) from exc
    except (workflows.WorkflowInvalid, ValueError) as exc:
        raise _invalid(exc) from exc
    return WorkflowRunResponse(execution_id=execution_id)
