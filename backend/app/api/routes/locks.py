"""Edit-lock endpoints.

Resource CRUD does not exist yet (templates and workflows arrive in a later
milestone), so these endpoints operate on a ``(resource_type, resource_id)`` pair
directly. That is enough to prove and test the locking contract now, and the same
service will be called by the CRUD routes later.

Any authenticated user may *read* who holds a lock, because a viewer needs to know
whether the thing is being edited. Only a user with edit permission (owner or an
explicit ``edit`` share) may acquire one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.db.session import get_session_factory
from app.models.audit_actions import AuditAction, AuditOutcome
from app.models.permission import ResourceType
from app.models.template import Template
from app.models.workflow import Workflow
from app.schemas.locks import (
    LockAcquireRequest,
    LockRead,
    LockRenewRequest,
    LockStateRead,
)
from app.services import audit, locks, permissions, sessions
from app.services.identity import client_ip, require_principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/locks", tags=["locks"])


# --------------------------------------------------------------------------- #
# Resource lookup
# --------------------------------------------------------------------------- #
# Templates and workflows are the only shareable resources. A resource that does
# not exist yet is reportable as "unlocked" so the UI can render the editor before
# the first save, but acquiring a lock on it still requires a valid owner row once
# it exists. Until CRUD lands, resources are created directly in tests.

def _resource(resource_type: ResourceType, resource_id: int):
    session = get_session_factory()()
    try:
        if resource_type is ResourceType.TEMPLATE:
            return session.get(Template, resource_id)
        return session.get(Workflow, resource_id)
    finally:
        session.close()


def _parse_type(value: str) -> ResourceType:
    try:
        return ResourceType(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown resource type {value!r}",
        ) from exc


def _lock_read(info: locks.LockInfo) -> LockRead:
    return LockRead(
        resource_type=info.resource_type,
        resource_id=info.resource_id,
        user_id=info.user_id,
        username=info.username,
        display_name=info.display_name,
        session_id=info.session_id,
        acquired_at=info.acquired_at,
        renewed_at=info.renewed_at,
        expires_at=info.expires_at,
        is_mine=info.is_mine,
    )


def _session_id_for(principal: sessions.Principal) -> int:
    """Locks are owned by a session, so a cookie session is required.

    In proxy mode there is no session row. Locks then key on the user alone, using
    a synthetic id of 0, because the proxy re-authenticates every request anyway.
    """
    return principal.session_id if principal.session_id is not None else 0


@router.get(
    "/{resource_type}/{resource_id}",
    response_model=LockStateRead,
    summary="Who is editing this resource",
)
def lock_state(
    resource_type: str,
    resource_id: int,
    principal: sessions.Principal = Depends(require_principal),
) -> LockStateRead:
    kind = _parse_type(resource_type)
    resource = _resource(kind, resource_id)
    owner_id = getattr(resource, "owner_id", None)
    row_version = getattr(resource, "row_version", None)

    can_view = resource is not None and permissions.can_view(
        kind, resource_id, owner_id, principal.user_id
    )
    can_edit_resource = resource is not None and permissions.can_edit(
        kind, resource_id, owner_id, principal.user_id
    )

    holder = locks.current_holder(kind, resource_id, user_id=principal.user_id)
    return LockStateRead(
        resource_type=kind.value,
        resource_id=resource_id,
        locked=holder is not None,
        lock=_lock_read(holder) if holder else None,
        can_edit=can_edit_resource,
        can_view=can_view,
        row_version=row_version,
    )


@router.post(
    "/{resource_type}/{resource_id}",
    response_model=LockStateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Acquire the edit lock",
    responses={409: {"description": "Another editor holds the lock"}},
)
def acquire_lock(
    resource_type: str,
    resource_id: int,
    request: Request,
    payload: LockAcquireRequest | None = None,
    principal: sessions.Principal = Depends(require_principal),
) -> LockStateRead:
    kind = _parse_type(resource_type)
    resource = _resource(kind, resource_id)
    if resource is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")

    try:
        permissions.require_edit(kind, resource_id, resource.owner_id, principal.user_id)
    except permissions.NotAuthorized as exc:
        audit.record(
            AuditAction.ACCESS_DENIED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            outcome=AuditOutcome.DENIED,
            resource_type=kind.value,
            resource_id=resource_id,
            session_id=principal.session_id,
            remote_addr=client_ip(request),
            detail={"reason": "lock_denied", "detail": exc.detail},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail
        ) from exc

    ttl = payload.ttl_minutes if payload and payload.ttl_minutes else locks.LOCK_TTL_MINUTES
    try:
        info = locks.acquire(
            kind,
            resource_id,
            user_id=principal.user_id,
            session_id=_session_id_for(principal),
            ttl_minutes=ttl,
            remote_addr=client_ip(request),
        )
    except locks.LockConflict as exc:
        audit.record(
            AuditAction.LOCK_ACQUIRED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            outcome=AuditOutcome.DENIED,
            resource_type=kind.value,
            resource_id=resource_id,
            session_id=principal.session_id,
            remote_addr=client_ip(request),
            detail={"reason": "held_by_another", "holder_user_id": exc.holder.user_id},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"{exc.holder.holder_label} is currently editing this resource."
                ),
                "lock": _lock_read(exc.holder).model_dump(mode="json"),
            },
        ) from exc

    return LockStateRead(
        resource_type=kind.value,
        resource_id=resource_id,
        locked=True,
        lock=_lock_read(info),
        can_edit=True,
        can_view=True,
        row_version=getattr(resource, "row_version", None),
    )


@router.put(
    "/{resource_type}/{resource_id}/renew",
    response_model=LockRead,
    summary="Renew my lock",
    responses={409: {"description": "You do not hold the lock"}},
)
def renew_lock(
    resource_type: str,
    resource_id: int,
    payload: LockRenewRequest | None = None,
    principal: sessions.Principal = Depends(require_principal),
) -> LockRead:
    kind = _parse_type(resource_type)
    ttl = payload.ttl_minutes if payload and payload.ttl_minutes else locks.LOCK_TTL_MINUTES
    try:
        info = locks.renew(
            kind,
            resource_id,
            user_id=principal.user_id,
            session_id=_session_id_for(principal),
            ttl_minutes=ttl,
        )
    except (locks.LockRequired, locks.LockConflict) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _lock_read(info)


@router.delete(
    "/{resource_type}/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Release my lock",
)
def release_lock(
    resource_type: str,
    resource_id: int,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
):
    kind = _parse_type(resource_type)
    released = locks.release(
        kind,
        resource_id,
        user_id=principal.user_id,
        session_id=_session_id_for(principal),
    )
    if not released:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You do not hold the lock for this resource.",
        )
    return None


# --------------------------------------------------------------------------- #
# Shares
# --------------------------------------------------------------------------- #

@router.post(
    "/{resource_type}/{resource_id}/shares",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Share a resource I own",
)
def share_resource(
    resource_type: str,
    resource_id: int,
    request: Request,
    user_id: int,
    level: str = "view",
    principal: sessions.Principal = Depends(require_principal),
):
    """Grant another user ``view`` or ``edit``.

    Only the owner may share. Ownership is read from the row, never from the
    request, so a shared editor cannot re-share at a higher level.
    """
    kind = _parse_type(resource_type)
    resource = _resource(kind, resource_id)
    if resource is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    if resource.owner_id != principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner can share this resource.",
        )
    try:
        parsed = permissions.PermissionLevel(level)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="level must be 'view' or 'edit'",
        ) from exc

    permissions.grant(
        kind, resource_id, user_id=user_id, level=parsed, granted_by_id=principal.user_id
    )
    audit.record(
        AuditAction.RESOURCE_UPDATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type=kind.value,
        resource_id=resource_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={"shared_with_user_id": user_id, "level": parsed.value},
    )
    return None


@router.delete(
    "/{resource_type}/{resource_id}/shares/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a share",
)
def unshare_resource(
    resource_type: str,
    resource_id: int,
    user_id: int,
    principal: sessions.Principal = Depends(require_principal),
):
    kind = _parse_type(resource_type)
    resource = _resource(kind, resource_id)
    if resource is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    if resource.owner_id != principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner can revoke a share.",
        )
    if not permissions.revoke(kind, resource_id, user_id=user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
    return None
