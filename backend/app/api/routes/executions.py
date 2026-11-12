"""Execution endpoints.

Routes: start, list, read, host transcript, cancel. ``get_log_store`` is read
here so a run's filesystem transcript can be viewed from the UI; the bytes stay
on disk and SQLite keeps only metadata.

Every route requires an authenticated actor and is scoped to executions that actor
created. An execution is SSH work against real hosts: letting another user read
its output, or cancel it, would be both a data leak and a denial of service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

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
    HostRunRead,
    HostTranscriptRead,
    StepRunRead,
)
from app.services import audit, execution_state, sessions
from app.services.execution_state import HostSpec, StepSpec
from app.services.identity import (
    client_ip,
    require_principal,
    require_same_origin,
    resolve_principal,
)
from app.services.log_broadcast import StatusEvent, drain_dropped, get_broadcaster
from app.services.log_store import get_log_store

logger = logging.getLogger(__name__)

# A run in one of these states may still change and may be cancelled. Everything
# else is final, so a cancel request against it is a no-op rather than a success.
_NON_TERMINAL_STATUSES = frozenset(
    {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}
)

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
    execution_id: int,
    principal: sessions.Principal,
    *,
    action: str,
    request: HTTPConnection,
):
    """Load an execution the caller created, or answer 404.

    404 rather than 403 on purpose: confirming that somebody else's execution id
    exists is itself a small information leak, and the UI has no use for it.

    Typed on ``HTTPConnection`` so the transcript WebSocket shares this exact
    check instead of re-deriving it; only ``.client`` is read, which both provide.
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
                schedule_id=row.schedule_id,
                schedule_name=row.schedule_name,
                scheduled_for=row.scheduled_for,
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
    from app.services.execution_runner import get_runner

    execution = _owned_execution(
        execution_id, principal, action="read", request=request
    )

    return ExecutionRead(
        id=execution.id,
        workflow_name=execution.workflow_name,
        status=execution.status,
        trigger=execution.trigger.value,
        schedule_id=execution.schedule_id,
        schedule_name=execution.schedule_name,
        scheduled_for=execution.scheduled_for,
        host_count=execution.host_count,
        succeeded_host_count=execution.succeeded_host_count,
        failed_host_count=execution.failed_host_count,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        created_at=execution.created_at,
        log_path=execution.log_path,
        error_message=execution.error_message,
        cancel_requested_at=execution.cancel_requested_at,
        cancel_requested_by_id=execution.cancel_requested_by_id,
        cancel_reason=execution.cancel_reason,
        is_active=(
            execution.status in _NON_TERMINAL_STATUSES
            and get_runner().is_active(execution.id)
        ),
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

    # Persist first, notify second. If the runner is never reached -- the task has
    # already ended, or this process did not start the run -- the request is still
    # in the database and therefore still visible in history, which is what makes
    # a cancellation observable rather than a no-op.
    recorded, current = execution_state.request_cancel(
        execution_id,
        reason="cancelled by request",
        requested_by_id=principal.user_id,
    )
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found"
        )

    if recorded:
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

    # Cooperative signal to whichever host workflows are still alive. This does
    # not kill a remote process; see ``CancelToken`` for what actually happens to
    # an in-flight SSH command.
    runner = get_runner()
    signalled = runner.request_cancel(execution_id, "cancelled by request")

    if recorded and not signalled and not runner.is_active(execution_id):
        # Nothing in this process is running the row, so the cooperative signal
        # reached nobody. Settle it here; otherwise it stays RUNNING for good and
        # cannot be cancelled again because the request timestamp already exists.
        execution_state.settle_unowned_cancel(
            execution_id, reason="cancelled by request"
        )
        get_broadcaster().publish_status(
            execution_id, ExecutionStatus.CANCELLED.value, reason="cancelled by request"
        )
        current = ExecutionStatus.CANCELLED

    return CancelResponse(
        id=execution_id, status=current, cancel_requested=recorded
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


# --------------------------------------------------------------------------- #
# Live transcript streaming
# --------------------------------------------------------------------------- #

# Matches the HTTP transcript route's ceiling. The UI renders a tail, so this
# bounds both the replay payload and what one client can pull into memory.
_REPLAY_LIMIT_BYTES = 256_000

# Idle re-check of the transcript file. Live output normally arrives through the
# broadcaster; this covers bytes written before this process published them (an
# app restart mid-run), so a tail cannot silently stop growing.
_STREAM_POLL_SECONDS = 1.0

# A host in any of these states will never write another byte, so a stream ends
# after delivering what is on disk instead of holding the socket open forever.
_TERMINAL_HOST_STATUSES = frozenset(
    {"succeeded", "failed", "skipped", "cancelled", "terminated"}
)


async def _deny_websocket(websocket: WebSocket, status_code: int, detail: str) -> None:
    """Refuse a WebSocket before acceptance, with a real HTTP status.

    Denying is better than accepting and immediately closing: the client learns
    why, and a policy failure cannot be mistaken for a stream that ended
    normally.
    """
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    with contextlib.suppress(RuntimeError):  # already responded or disconnected
        await websocket.send_denial_response(response)


async def _authorize_stream(
    websocket: WebSocket, execution_id: int, host_run_id: int
) -> tuple[str, str] | None:
    """Authenticate and authorize a transcript stream, or deny and return None.

    Origin is checked first. The HTTP CSRF middleware only wraps ``http``
    requests, so a WebSocket upgrade never reaches it; without an explicit check
    a hostile page could open a socket with the victim's cookie and read their
    execution output.

    Ownership and host membership reuse the HTTP rules, including 404 rather
    than 403 so execution ids cannot be probed.
    """
    settings = websocket.app.state.settings

    try:
        require_same_origin(websocket, settings=settings)
    except HTTPException as exc:
        audit.record(
            AuditAction.ACCESS_DENIED,
            actor_id=None,
            actor_username=None,
            outcome=AuditOutcome.DENIED,
            resource_type="execution",
            resource_id=execution_id,
            remote_addr=client_ip(websocket),
            detail={"reason": "cross_origin_websocket", "attempted": "stream_transcript"},
        )
        await _deny_websocket(websocket, exc.status_code, str(exc.detail))
        return None

    try:
        principal = resolve_principal(websocket)
    except HTTPException as exc:
        await _deny_websocket(websocket, exc.status_code, str(exc.detail))
        return None

    try:
        execution = _owned_execution(
            execution_id, principal, action="stream_transcript", request=websocket
        )
    except HTTPException as exc:
        await _deny_websocket(websocket, exc.status_code, str(exc.detail))
        return None

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
            remote_addr=client_ip(websocket),
            detail={"reason": "host_run_not_in_execution", "attempted": "stream_transcript"},
        )
        await _deny_websocket(websocket, status.HTTP_404_NOT_FOUND, "Host run not found")
        return None

    return host.hostname, str(host.status)


def _tail_window(execution_id: int, host_run_id: int, hostname: str) -> tuple[bytes, int, bool]:
    """Read the replayable tail of a transcript as ``(bytes, size, truncated)``.

    Bytes rather than text: offsets are byte offsets, so a character-based
    window would not line up with the boundary the client is deduplicating on.
    """
    path = get_log_store().host_log_path(execution_id, host_run_id, hostname)
    try:
        size = path.stat().st_size
    except OSError:
        return b"", 0, False
    if size <= 0:
        return b"", 0, False

    truncated = size > _REPLAY_LIMIT_BYTES
    offset = size - _REPLAY_LIMIT_BYTES if truncated else 0
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size - offset), size, truncated


def _trim_to_utf8_boundary(data: bytes, limit: int) -> tuple[bytes, int]:
    """Cut ``data`` to ``limit`` bytes starting on a character boundary.

    The window starts at an arbitrary byte offset, so its first byte may be the
    middle of a multi-byte character. Decoding that would emit U+FFFD and
    silently corrupt the transcript, so leading continuation bytes are skipped.
    """
    if len(data) <= limit:
        return data, 0
    start = len(data) - limit
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return data[start:], start


def _drop_leading_bytes(text: str, count: int) -> tuple[str, int]:
    """Remove the first ``count`` bytes from ``text``.

    Used to trim the part of a live frame the replay already delivered. Slicing
    the *string* would be wrong: ``count`` is a byte offset and may land inside a
    multi-byte character. Returns the remaining text and the number of bytes
    actually removed.
    """
    if count <= 0:
        return text, 0
    encoded = text.encode("utf-8", "replace")
    if count >= len(encoded):
        return "", len(encoded)
    removed = count
    while removed < len(encoded) and (encoded[removed] & 0xC0) == 0x80:
        removed += 1
    return encoded[removed:].decode("utf-8", "replace"), removed


async def _forward_live(
    websocket: WebSocket,
    subscription,
    *,
    execution_id: int,
    host_run_id: int,
    boundary: int,
    hostname: str,
    terminal: bool,
) -> None:
    """Forward frames past ``boundary`` until the socket closes or the host ends.

    ``boundary`` is the file size already sent by the replay. Frames that end at
    or before it were produced while the replay was being read and are dropped;
    a frame straddling it is trimmed, so no byte is delivered twice.
    """
    sent = boundary
    while True:
        try:
            frame = await asyncio.wait_for(
                subscription.queue.get(), timeout=_STREAM_POLL_SECONDS
            )
        except TimeoutError:
            frame = None

        if frame is not None:
            if isinstance(frame, StatusEvent):
                # Not transcript bytes: no offsets, so it can never be trimmed or
                # deduplicated against `sent`. Forwarded as-is, before the exit
                # handling below decides whether the stream is finished.
                await websocket.send_json(
                    {
                        "type": "status",
                        "status": frame.status,
                        "reason": frame.reason,
                        "cancel_requested_at": frame.cancel_requested_at,
                    }
                )
                continue
            if frame.end <= sent:
                # Produced during the replay read; already on the wire.
                continue
            text, start = frame.text, frame.start
            if frame.start < sent:
                # Trim only the overlapping head so no byte is sent twice.
                text, removed = _drop_leading_bytes(text, sent - frame.start)
                start = frame.start + removed
                if not text:
                    continue
            await websocket.send_json(
                {"type": "chunk", "content": text, "start": start, "end": frame.end}
            )
            sent = max(sent, frame.end)
            continue

        dropped_chunks, dropped_bytes = drain_dropped(subscription)
        if dropped_chunks:
            await websocket.send_json(
                {
                    "type": "truncated",
                    "reason": "subscriber_overflow",
                    "dropped_chunks": dropped_chunks,
                    "dropped_bytes": dropped_bytes,
                }
            )

        # Nothing live arrived within the poll interval. If the host is finished
        # it will never write again, so deliver any tail still missing (bytes
        # produced before this process published them, e.g. after a restart) and
        # end the stream rather than holding the socket open.
        if terminal:
            size = _tail_window(execution_id, host_run_id, hostname)[1]
            if size > sent:
                await websocket.send_json(
                    {
                        "type": "truncated",
                        "reason": "stream_ended",
                        "missing_bytes": size - sent,
                    }
                )
            await websocket.send_json({"type": "end"})
            return


@router.websocket("/{execution_id}/hosts/{host_run_id}/ws")
async def stream_host_transcript(
    websocket: WebSocket, execution_id: int, host_run_id: int
) -> None:
    """Follow one host run's transcript as it is written.

    Every transcript byte -- stdout, stderr and engine notes -- passes through
    ``HostTranscript._raw``, which publishes it here with absolute file offsets.
    This connection replays what already exists, then forwards live frames,
    deduplicated by those offsets so output produced during the replay is
    delivered exactly once.

    The order is what makes it race-free: subscribing after reading the file
    would lose anything written in between. Subscribing first and replaying to a
    captured boundary can only duplicate, which the offsets then remove.
    """
    authorization = await _authorize_stream(websocket, execution_id, host_run_id)
    if authorization is None:
        return
    hostname, host_status = authorization

    broadcaster = get_broadcaster()
    subscription = broadcaster.subscribe(execution_id, host_run_id)
    try:
        await websocket.accept()

        replay, file_size, truncated = _tail_window(execution_id, host_run_id, hostname)
        replay, skipped = _trim_to_utf8_boundary(replay, _REPLAY_LIMIT_BYTES)
        await websocket.send_json(
            {
                "type": "replay",
                "content": replay.decode("utf-8", errors="replace"),
                "start": file_size - len(replay),
                "end": file_size,
                "truncated": truncated or skipped > 0,
            }
        )

        await _forward_live(
            websocket,
            subscription,
            execution_id=execution_id,
            host_run_id=host_run_id,
            boundary=file_size,
            hostname=hostname,
            terminal=host_status in _TERMINAL_HOST_STATUSES,
        )
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # Shutdown or client abort. Reraise to preserve cancellation semantics;
        # the finally below still unsubscribes.
        raise
    finally:
        broadcaster.unsubscribe(execution_id, host_run_id, subscription)
