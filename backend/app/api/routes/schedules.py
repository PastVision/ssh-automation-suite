"""Schedule endpoints.

The same conventions as the workflow routes, because they are the same kind of
resource:

* a schedule the caller cannot see answers **404**, never 403;
* a write with a stale ``row_version`` answers **409**;
* a definition that cannot form a usable schedule answers **422**.

One rule is specific to scheduling: nothing here dispatches. Creating a schedule
computes its first firing and stores it; the scheduler loop picks it up from there.
That keeps the HTTP path fast and makes "did it run?" answerable from the ledger
alone rather than from request logs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.models.schedule import Schedule, ScheduleKind
from app.schemas.schedule import (
    PreviewFiring,
    ScheduleEnabledInput,
    ScheduleInput,
    ScheduleListResponse,
    SchedulePreviewRequest,
    SchedulePreviewResponse,
    ScheduleRead,
    ScheduleRunListResponse,
    ScheduleRunRead,
    ScheduleUpdate,
)
from app.services import schedules, sessions
from app.services.identity import client_ip, require_principal
from app.services.timezones import build_trigger, upcoming_firings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedules", tags=["schedules"])


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #


def _data(payload: ScheduleInput) -> schedules.ScheduleData:
    return schedules.ScheduleData(
        name=payload.name,
        workflow_id=payload.workflow_id,
        schedule_kind=payload.schedule_kind,
        timezone=payload.timezone,
        cron_expression=payload.cron_expression,
        run_at=payload.run_at,
        misfire_policy=payload.misfire_policy,
        misfire_grace_seconds=payload.misfire_grace_seconds,
    )


def _read(schedule: Schedule) -> ScheduleRead:
    return ScheduleRead.model_validate(schedule)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found"
    )


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _invalid(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("", response_model=ScheduleListResponse, summary="List schedules I can see")
def list_schedules(
    workflow_id: int | None = Query(default=None),
    principal: sessions.Principal = Depends(require_principal),
) -> ScheduleListResponse:
    items = schedules.list_schedules(principal=principal, workflow_id=workflow_id)
    return ScheduleListResponse(items=[_read(item) for item in items], total=len(items))


@router.post(
    "",
    response_model=ScheduleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a schedule",
)
def create_schedule(
    payload: ScheduleInput,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> ScheduleRead:
    try:
        schedule = schedules.create(
            _data(payload), principal=principal, remote_addr=client_ip(request)
        )
    except schedules.ScheduleConflict as exc:
        raise _conflict(exc) from exc
    except schedules.ScheduleWorkflowMissing as exc:
        # The target workflow is invisible or absent: the same 404 the workflow
        # routes give, so this endpoint cannot be used to probe for workflows.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        ) from exc
    except (schedules.ScheduleInvalid, ValueError) as exc:
        raise _invalid(exc) from exc
    return _read(schedule)


@router.post(
    "/preview",
    response_model=SchedulePreviewResponse,
    summary="Preview the next firings of a timing definition",
)
def preview_schedule(
    payload: SchedulePreviewRequest,
    principal: sessions.Principal = Depends(require_principal),
) -> SchedulePreviewResponse:
    """Show a user when their cron expression will actually fire.

    Previewing is a pure calculation: nothing is stored, and no principal-scoped
    resource is read. It exists because a cron expression is the one part of this
    feature a user cannot verify by reading it.
    """
    try:
        trigger = build_trigger(
            timezone=payload.timezone,
            cron_expression=payload.cron_expression,
            run_at=payload.run_at,
        )
    except Exception as exc:
        raise _invalid(exc) from exc

    from datetime import UTC, datetime

    firings = upcoming_firings(
        trigger, after=datetime.now(UTC), timezone=payload.timezone, count=payload.count
    )
    if payload.schedule_kind is ScheduleKind.ONCE:
        firings = firings[:1]
    return SchedulePreviewResponse(
        items=[
            PreviewFiring(scheduled_for=firing.scheduled_for, local_time=firing.local_time)
            for firing in firings
        ]
    )


@router.get("/{schedule_id}", response_model=ScheduleRead, summary="Read a schedule")
def read_schedule(
    schedule_id: int,
    principal: sessions.Principal = Depends(require_principal),
) -> ScheduleRead:
    try:
        schedule = schedules.get_schedule(schedule_id, principal=principal)
    except schedules.ScheduleNotFound as exc:
        raise _not_found() from exc
    return _read(schedule)


@router.put("/{schedule_id}", response_model=ScheduleRead, summary="Replace a schedule")
def update_schedule(
    schedule_id: int,
    payload: ScheduleUpdate,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> ScheduleRead:
    try:
        schedule = schedules.update(
            schedule_id,
            _data(payload),
            principal=principal,
            remote_addr=client_ip(request),
            expected_row_version=payload.row_version,
        )
    except schedules.ScheduleNotFound as exc:
        raise _not_found() from exc
    except schedules.ScheduleWorkflowMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        ) from exc
    except schedules.ScheduleConflict as exc:
        raise _conflict(exc) from exc
    except (schedules.ScheduleInvalid, ValueError) as exc:
        raise _invalid(exc) from exc
    return _read(schedule)


@router.post(
    "/{schedule_id}/enabled",
    response_model=ScheduleRead,
    summary="Pause or resume a schedule",
)
def set_schedule_enabled(
    schedule_id: int,
    payload: ScheduleEnabledInput,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> ScheduleRead:
    try:
        schedule = schedules.set_enabled(
            schedule_id,
            payload.enabled,
            principal=principal,
            remote_addr=client_ip(request),
            expected_row_version=payload.row_version,
        )
    except schedules.ScheduleNotFound as exc:
        raise _not_found() from exc
    except schedules.ScheduleConflict as exc:
        raise _conflict(exc) from exc
    return _read(schedule)


@router.delete(
    "/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a schedule",
)
def delete_schedule(
    schedule_id: int,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> None:
    try:
        schedules.delete(schedule_id, principal=principal, remote_addr=client_ip(request))
    except schedules.ScheduleNotFound as exc:
        raise _not_found() from exc


@router.get(
    "/{schedule_id}/runs",
    response_model=ScheduleRunListResponse,
    summary="Read a schedule's firing history",
)
def read_schedule_runs(
    schedule_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: sessions.Principal = Depends(require_principal),
) -> ScheduleRunListResponse:
    """Every firing time and what came of it, newest first.

    This is where "why did my workflow not run at 03:00?" is answered: a skipped
    firing is a row with a reason, not an absence.
    """
    try:
        history = schedules.run_history(
            schedule_id, principal=principal, limit=limit, offset=offset
        )
    except schedules.ScheduleNotFound as exc:
        raise _not_found() from exc
    return ScheduleRunListResponse(
        items=[ScheduleRunRead.model_validate(row) for row in history.items],
        total=history.total,
    )
