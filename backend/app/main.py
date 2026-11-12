"""FastAPI application factory.

Kept as a factory (rather than a module-level ``app``) so tests can build an app
against temporary directories without touching real data.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import audit as audit_routes
from app.api.routes import auth as auth_routes
from app.api.routes import credentials as credential_routes
from app.api.routes import executions, health
from app.api.routes import locks as lock_routes
from app.api.routes import profile as profile_routes
from app.api.routes import schedules as schedule_routes
from app.api.routes import templates as template_routes
from app.api.routes import workflows as workflow_routes
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.services.identity import csrf_protector

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.ensure_directories()

    # A runner whose shutdown has completed belongs to a previous application
    # instance and is permanently closed. Adopting it would make every new
    # execution answer 503, so start this instance with a fresh one. (In a normal
    # deployment the process starts once; this matters for restarts in-process,
    # in tests, and for any embedder that builds more than one app.)
    from app.services import execution_runner

    if execution_runner.RUNNER_SINGLETON is not None and execution_runner.get_runner().closing:
        execution_runner.reset_runner()

    # Schema is owned by Alembic; warn rather than auto-create so a forgotten
    # `alembic upgrade head` cannot silently diverge from the migrations.
    from sqlalchemy import inspect

    from app.db.session import get_engine

    tables = inspect(get_engine()).get_table_names()
    if "alembic_version" not in tables:
        logger.warning(
            "Database has no alembic_version table. Run 'alembic upgrade head' "
            "from the backend directory before using the API."
        )

    logger.info(
        "%s %s started (env=%s, db=%s)",
        settings.app_name,
        settings.app_version,
        settings.environment,
        settings.resolved_database_url,
    )

    # Scheduling is opt-in: a single process may run the loop, but a deployment is
    # free to run none (tests, workers, or a process dedicated to HTTP). The loop
    # takes a lease, so enabling it everywhere is still safe -- only one holder
    # dispatches -- but there is no reason to pay for it where it is not wanted.
    from app.services import scheduler as scheduler_service

    scheduler_task = None
    if settings.scheduler_enabled:
        scheduler_service.reset_scheduler()
        scheduler_task = scheduler_service.get_scheduler().start()
        logger.info("Scheduler started")
    else:
        logger.info("Scheduler disabled (SSHSUITE_SCHEDULER_ENABLED=false)")

    try:
        yield
    finally:
        if scheduler_task is not None:
            await scheduler_service.get_scheduler().stop()
            scheduler_service.reset_scheduler()
        # Stop accepting work, let host workflows finish, then cancel what remains.
        # This runs inside the lifespan so final execution states are persisted
        # before the process exits.
        from app.services.execution_runner import get_runner

        await get_runner().shutdown()
        logger.info("Application shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    settings.ensure_directories()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(auth_routes.router, prefix=settings.api_prefix)
    app.include_router(profile_routes.router, prefix=settings.api_prefix)
    app.include_router(lock_routes.router, prefix=settings.api_prefix)
    app.include_router(audit_routes.router, prefix=settings.api_prefix)
    app.include_router(template_routes.router, prefix=settings.api_prefix)
    app.include_router(credential_routes.router, prefix=settings.api_prefix)
    app.include_router(workflow_routes.router, prefix=settings.api_prefix)
    app.include_router(executions.router, prefix=settings.api_prefix)
    app.include_router(schedule_routes.router, prefix=settings.api_prefix)

    @app.middleware("http")
    async def _csrf_guard(request: Request, call_next):
        """Reject unsafe requests that lack a CSRF token.

        Implemented as middleware rather than a per-route dependency so a future
        route cannot be added without the protection by forgetting to declare it.
        It only inspects requests under the API prefix; CORS preflight and static
        assets are untouched.
        """
        if request.url.path.startswith(settings.api_prefix):
            try:
                csrf_protector(request)
            except Exception as exc:  # HTTPException from the protector
                status_code = getattr(exc, "status_code", 403)
                detail = getattr(exc, "detail", "Request rejected.")
                return JSONResponse(
                    status_code=status_code, content={"detail": detail}
                )
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "health": f"{settings.api_prefix}/health",
            "docs": "/docs",
        }

    return app


app = create_app()
