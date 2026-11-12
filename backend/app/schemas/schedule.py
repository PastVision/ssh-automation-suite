"""Schedule request/response models.

One shape covers all schedule kinds: ``schedule_kind`` says which timing field is
authoritative and the other is ignored, exactly as the service validates it.
Splitting into a discriminated union would buy stricter typing at the cost of a
second, third and fourth client code path, so the flat form is deliberate.

``row_version`` is exposed because pause/resume and edits are guarded by it; a
client that ignores it will simply get a 409 rather than silently overwriting
someone else's change.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.schedule import (
    MisfirePolicy,
    PausedReason,
    ScheduleKind,
    ScheduleRunOutcome,
    ScheduleState,
)


class ScheduleInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    workflow_id: int
    schedule_kind: ScheduleKind
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    cron_expression: str | None = Field(default=None, max_length=120)
    run_at: datetime | None = None
    misfire_policy: MisfirePolicy = MisfirePolicy.SKIP
    misfire_grace_seconds: int = Field(default=300, ge=0, le=86400)


class ScheduleUpdate(ScheduleInput):
    row_version: int


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    workflow_id: int | None
    workflow_name: str
    name: str
    schedule_kind: ScheduleKind
    timezone: str
    cron_expression: str | None
    run_at: datetime | None
    state: ScheduleState
    enabled: bool
    paused_reason: PausedReason | None
    misfire_policy: MisfirePolicy
    misfire_grace_seconds: int
    last_fired_for: datetime | None
    next_run_at: datetime | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class ScheduleListResponse(BaseModel):
    items: list[ScheduleRead]
    total: int


class ScheduleEnabledInput(BaseModel):
    enabled: bool
    row_version: int


class ScheduleRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    schedule_id: int
    execution_id: int | None
    scheduled_for: datetime
    outcome: ScheduleRunOutcome
    detail: str | None
    created_at: datetime


class ScheduleRunListResponse(BaseModel):
    items: list[ScheduleRunRead]
    total: int


class PreviewFiring(BaseModel):
    scheduled_for: datetime
    local_time: datetime


class SchedulePreviewResponse(BaseModel):
    """The next few firings, for showing a user what their cron actually means."""

    items: list[PreviewFiring]


class SchedulePreviewRequest(BaseModel):
    schedule_kind: ScheduleKind
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    cron_expression: str | None = Field(default=None, max_length=120)
    run_at: datetime | None = None
    count: int = Field(default=5, ge=1, le=20)
