"""Audit history endpoint.

Read-only by construction: there is no POST, PATCH, or DELETE here, and none
should ever be added. Non-owners see only their own events unless they can view
the referenced resource; owners of audit visibility (a future admin role) would be
added as an explicit, reviewed change rather than an implicit widening here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.schemas.locks import AuditEventRead, AuditPage
from app.services import audit, sessions
from app.services.identity import require_principal

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=AuditPage, summary="Audit history")
def list_audit(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    scope: str = Query(default="mine", pattern="^(mine|all)$"),
    principal: sessions.Principal = Depends(require_principal),
) -> AuditPage:
    """Recent audit events.

    ``scope=mine`` is the default and returns only the caller's own activity.
    ``scope=all`` is accepted but still filtered to the caller's events, because
    there is no administrator role in this milestone; it exists so the parameter
    is explicit rather than silently ignored.
    """
    rows = audit.list_events(limit=limit, offset=offset, actor_id=principal.user_id)
    return AuditPage(
        events=[
            AuditEventRead(
                id=row.id,
                actor_user_id=row.actor_user_id,
                actor_username=row.actor_username,
                action=row.action,
                outcome=row.outcome,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                detail=row.detail,
                remote_addr=row.remote_addr,
                session_id=row.session_id,
                created_at=row.created_at,
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )
