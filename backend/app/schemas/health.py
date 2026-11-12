"""Shared API schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthDatabase(BaseModel):
    ok: bool
    url_scheme: str
    error: str | None = None


class HealthConcurrency(BaseModel):
    limit: int = Field(ge=1)
    in_use: int = Field(ge=0)
    available: int = Field(ge=0)


class HealthPaths(BaseModel):
    data_dir: str
    log_dir: str
    execution_log_dir: str
    private_key_dir: str


class HealthScheduler(BaseModel):
    """Scheduler liveness. `enabled` is configuration; the rest is observation."""

    running: bool
    lease_held: bool
    generation: int | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    version: str
    environment: str
    scheduler_enabled: bool
    scheduler: HealthScheduler
    database: HealthDatabase
    concurrency: HealthConcurrency
    paths: HealthPaths
