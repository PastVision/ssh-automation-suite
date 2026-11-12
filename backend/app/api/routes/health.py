"""Health check endpoint.

Reports whether the pieces the app cannot run without are actually usable:
the SQLite file, the storage directories, and the concurrency budget. Returns
503 when the database is unreachable so orchestration layers can react.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.session import check_database
from app.schemas.health import (
    HealthConcurrency,
    HealthDatabase,
    HealthPaths,
    HealthResponse,
    HealthScheduler,
)
from app.services.concurrency import get_limiter
from app.services.scheduler import scheduler_status

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Application health",
    responses={503: {"model": HealthResponse, "description": "Database unreachable"}},
)
def health(response: Response) -> HealthResponse:
    settings = get_settings()

    db_ok, db_error = check_database()
    limiter = get_limiter()
    budget = limiter.snapshot()
    scheduler = scheduler_status()

    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        scheduler_enabled=settings.scheduler_enabled,
        scheduler=HealthScheduler(
            running=scheduler.running,
            lease_held=scheduler.lease_held,
            generation=scheduler.generation,
        ),
        database=HealthDatabase(
            ok=db_ok,
            url_scheme=make_url(settings.resolved_database_url).drivername,
            error=db_error,
        ),
        concurrency=HealthConcurrency(
            limit=budget.limit, in_use=budget.in_use, available=budget.available
        ),
        paths=HealthPaths(
            data_dir=str(settings.data_dir),
            log_dir=str(settings.log_dir),
            execution_log_dir=str(settings.execution_log_dir),
            private_key_dir=str(settings.private_key_dir),
        ),
    )
