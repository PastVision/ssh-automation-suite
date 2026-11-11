"""Edit-lock and audit API models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LockRead(BaseModel):
    resource_type: str
    resource_id: int
    user_id: int
    username: str
    display_name: str | None = None
    session_id: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    is_mine: bool


class LockStateRead(BaseModel):
    """Answer to "who is editing this?" -- always safe to show to any viewer."""

    resource_type: str
    resource_id: int
    locked: bool
    lock: LockRead | None = None
    can_edit: bool
    can_view: bool
    row_version: int | None = None


class LockAcquireRequest(BaseModel):
    ttl_minutes: int | None = Field(default=None, ge=1, le=120)


class LockRenewRequest(BaseModel):
    ttl_minutes: int | None = Field(default=None, ge=1, le=120)


class AuditEventRead(BaseModel):
    id: int
    actor_user_id: int | None = None
    actor_username: str | None = None
    action: str
    outcome: str
    resource_type: str | None = None
    resource_id: int | None = None
    detail: str | None = None
    remote_addr: str | None = None
    session_id: int | None = None
    created_at: datetime


class AuditPage(BaseModel):
    events: list[AuditEventRead]
    limit: int
    offset: int
