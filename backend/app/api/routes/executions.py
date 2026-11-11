"""Execution endpoints.

Routes: start, list, read, host transcript, cancel. ``get_log_store`` is read
here so a run's filesystem transcript can be viewed from the UI; the bytes stay
on disk and SQLite keeps only metadata.

Every route requires an authenticated actor and is scoped to executions that actor
created. An execution is SSH work against real hosts: letting another user read
its output, or cancel it, would be both a data leak and a denial of service.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.audit_actions import AuditAction, AuditOutcome
from app.models.credential import SshCredential
from app.models.enums import ExecutionStatus
from app.schemas.execution import (
    CancelResponse,
    ExecutionAccepted,
    ExecutionCreate,
    ExecutionListResponse,
    ExecutionRead,
    ExecutionSummary,
    HostTranscriptRead,
    HostRunRead,
    StepRunRead,
)
from app.services import audit, execution_state, sessions
from app.services.execution_state import HostSpec, StepSpec
from app.services.log_store import get_log_store
from app.services.identity import client_ip, require_principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/executions", tags=["executions"])


def _credential_for_user(credential_id: int, user_id: int) -> tuple[str, str, int]:
    """Return (name, username, default_port) for a credential the caller owns.

    Ownership is checked here rather than in the runner: this is the boundary
    between HTTP input and the execution engine. A credential belonging to another
    user is reported as missing, so the response does not confirm its existence.
    """
    session = get_session_factory()()
    try:
        credential = session.scalar(
            select(SshCredential).where(
                SshCredential.id == credential_id,
                SshCredential.owner_id == user_id,
            )
        )
        if credential is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Credential {credential_id} does not exist.",
            )
        if not credential.private_key_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Credential {credential_id} has no private key stored.",
            )
        return credential.name, credential.username, credential.default_port
    finally:
        session.close()


def _owned_execution(
    execution_id: int, principal: sessions.Principal, *, action: str, request: Request
):
    """Load an execution the caller created, or answer 404.

    404 rather than 403 on purpose: confirming that somebody else's execution id
    exists is itself a small information leak, and the UI has no use for it.
    """
    execution = execution_state.get_execution(execution_id)
    if execution is None or execution.created_by_id != principal.user_id:
        audit.record(
            AuditAction.ACCESS_DENIED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            outcome=AuditOutcome.DENIED,
            resource_type="execution",
            resource_id=execution_id,
            remote_addr=client_ip(request),
            detail={"reason": "not_execution_owner", "attempted": action},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found"
        )
    return execution


@router.post(
    "",
    response_model=ExecutionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an execution",
)
async def start_execution(
    payload: ExecutionCreate,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> ExecutionAccepted:
    from app.services.execution_runner import get_runner

    runner = get_runner()
    if runner.closing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The service is shutting down and cannot accept new executions.",
        )

    name, _username, default_port = _credential_for_user(
        payload.credential_id, principal.user_id
    )
    settings = get_settings()

    steps = [
        StepSpec(
            template_name=step.name,
            template_version=1,
            script=step.script,
            interpreter=step.interpreter,
            timeout_seconds=step.timeout_seconds or settings.ssh_command_timeout_seconds,
        )
        for step in payload.steps
    ]
    hosts = [
        HostSpec(
            hostname=host.hostname,
            port=host.port or default_port,
            credential_id=payload.credential_id,
            credential_name=name,
        )
        for host in payload.hosts
    ]

    execution_id = execution_state.create_execution(
        workflow_name=payload.workflow_name,
        steps=steps,
        hosts=hosts,
        max_concurrent_hosts=settings.max_concurrent_host_workflows,
        created_by_id=principal.user_id,
    )
    try:
        runner.start(execution_id)
    except RuntimeError as exc:
        execution_state.finish_execution(
            execution_id,
            status=ExecutionStatus.FAILED,
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    audit.record(
        AuditAction.EXECUTION_CREATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="execution",
        resource_id=execution_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={
            "workflow_name": payload.workflow_name,
            "credential_id": payload.credential_id,
            "hosts": len(hosts),
            "steps": len(steps),
        },
    )
    return ExecutionAccepted(id=execution_id, status=ExecutionStatus.PENDING)


@router.get(
    "",
    response_model=ExecutionListResponse,
    summary="My execution history, newest first",
)
def list_executions(
    status_filter: ExecutionStatus | None = Query(
        default=None, alias="status", description="Only runs in this state"
    ),
    workflow_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: sessions.Principal = Depends(require_principal),
) -> ExecutionListResponse:
    rows, total = execution_state.list_executions(
        created_by_id=principal.user_id,
        limit=limit,
        offset=offset,
        status=status_filter,
        workflow_id=workflow_id,
    )
    return ExecutionListResponse(
        items=[
            ExecutionSummary(
                id=row.id,
                workflow_id=row.workflow_id,
                workflow_name=row.workflow_name,
                status=row.status,
                trigger=row.trigger.value,
                host_count=row.host_count,
                succeeded_host_count=row.succeeded_host_count,
                failed_host_count=row.failed_host_count,
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
            )
            for row in rows
        ],
        total=total,
    )


@router.get("/{execution_id}", response_model=ExecutionRead, summary="Execution detail")
def read_execution(
    execution_id: int,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> ExecutionRead:
    execution = _owned_execution(
        execution_id, principal, action="read", request=request
    )

    return ExecutionRead(
        id=execution.id,
        workflow_name=execution.workflow_name,
        status=execution.status,
        trigger=execution.trigger.value,
        host_count=execution.host_count,
        succeeded_host_count=execution.succeeded_host_count,
        failed_host_count=execution.failed_host_count,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        created_at=execution.created_at,
        log_path=execution.log_path,
        error_message=execution.error_message,
        hosts=[
            HostRunRead(
                id=host.id,
                hostname=host.hostname,
                port=host.port,
                credential_name=host.credential_name,
                status=host.status,
                failed_step_position=host.failed_step_position,
                error_message=host.error_message,
                started_at=host.started_at,
                finished_at=host.finished_at,
                log_path=host.log_path,
                steps=[
                    StepRunRead(
                        position=step.position,
                        template_name=step.template_name,
                        status=step.status,
                        exit_code=step.exit_code,
                        duration_ms=step.duration_ms,
                        started_at=step.started_at,
                        finished_at=step.finished_at,
                    )
                    for step in host.step_runs
                ],
            )
            for host in sorted(execution.host_runs, key=lambda row: row.id)
        ],
    )


@router.post(
    "/{execution_id}/cancel",
    response_model=CancelResponse,
    summary="Request cancellation",
)
def cancel_execution(
    execution_id: int,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> CancelResponse:
    from app.services.execution_runner import get_runner

    # Cancelling is destructive and stops real remote work, so it needs the same
    # ownership check as reading.
    _owned_execution(execution_id, principal, action="cancel", request=request)

    current = execution_state.execution_status(execution_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")
    if current in (
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    ):
        return CancelResponse(
            id=execution_id, status=current, cancel_requested=False
        )

    requested = get_runner().request_cancel(execution_id, "cancelled by request")
    if requested:
        audit.record(
            AuditAction.EXECUTION_CANCELLED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            resource_type="execution",
            resource_id=execution_id,
            session_id=principal.session_id,
            remote_addr=client_ip(request),
            detail={"status_at_request": current.value},
        )
    return CancelResponse(
        id=execution_id, status=current, cancel_requested=requested
    )


@router.get(
    "/{execution_id}/hosts/{host_run_id}/transcript",
    response_model=HostTranscriptRead,
    summary="Filesystem transcript for one host run",
)
def read_host_transcript(
    execution_id: int,
    host_run_id: int,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> HostTranscriptRead:
    execution = _owned_execution(
        execution_id, principal, action="read_transcript", request=request
    )

    host = next((row for row in execution.host_runs if row.id == host_run_id), None)
    if host is None:
        audit.record(
            AuditAction.ACCESS_DENIED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            outcome=AuditOutcome.DENIED,
            resource_type="execution_host_run",
            resource_id=host_run_id,
            remote_addr=client_ip(request),
            detail={"reason": "host_run_not_in_execution"},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Host run not found"
        )

    text = get_log_store().read_host_log(execution_id, host.id, host.hostname)
    if text is None:
        # A host that has not started yet, or whose transcript was pruned. This is a
        # normal state rather than an error, so it answers 200 with available=False.
        return HostTranscriptRead(
            execution_id=execution_id,
            host_run_id=host.id,
            hostname=host.hostname,
            available=False,
        )

    # Bound the payload: a runaway command can produce a very large transcript and
    # the UI only renders the tail. The last bytes are the most useful for debugging.
    limit = 256_000
    truncated = len(text) > limit
    if truncated:
        text = text[-limit:]

    return HostTranscriptRead(
        execution_id=execution_id,
        host_run_id=host.id,
        hostname=host.hostname,
        available=True,
        truncated=truncated,
        content=text,
    )

