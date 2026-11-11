"""Minimal ownership and sharing authorization.

Deliberately not a role framework. Two rules:

* A resource belongs to exactly one owner. The owner always has full access.
* Another user needs an explicit ``ResourcePermission`` row, with ``view`` or
  ``edit``. There is no implicit "everyone can see everything".

Credentials and SSH profiles are *not* shared resources: they are private to their
owner with no sharing path at all, so those checks are ownership-only.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.permission import PermissionLevel, ResourcePermission, ResourceType

logger = logging.getLogger(__name__)


class NotAuthorized(Exception):
    """The caller may not perform this action."""

    def __init__(self, detail: str = "You do not have permission to do that.") -> None:
        super().__init__(detail)
        self.detail = detail


def permission_for(
    resource_type: ResourceType, resource_id: int, user_id: int
) -> PermissionLevel | None:
    """The explicit share granted to ``user_id``, if any."""
    session = get_session_factory()()
    try:
        row = session.scalar(
            select(ResourcePermission).where(
                ResourcePermission.resource_type == resource_type,
                ResourcePermission.resource_id == resource_id,
                ResourcePermission.user_id == user_id,
            )
        )
        return row.level if row else None
    finally:
        session.close()


# Access is decided by grants alone. There is deliberately no visibility argument
# here: a second policy living in this module is how the list query and the detail
# check drifted apart before. Revoking access means deleting the grant row.


def can_view(
    resource_type: ResourceType,
    resource_id: int,
    owner_id: int | None,
    user_id: int,
) -> bool:
    if owner_id is not None and owner_id == user_id:
        return True
    return permission_for(resource_type, resource_id, user_id) is not None


def can_edit(
    resource_type: ResourceType,
    resource_id: int,
    owner_id: int | None,
    user_id: int,
) -> bool:
    if owner_id is not None and owner_id == user_id:
        return True
    return permission_for(resource_type, resource_id, user_id) is PermissionLevel.EDIT


def require_view(
    resource_type: ResourceType,
    resource_id: int,
    owner_id: int | None,
    user_id: int,
) -> None:
    if not can_view(resource_type, resource_id, owner_id, user_id):
        raise NotAuthorized("You do not have access to this resource.")


def require_edit(
    resource_type: ResourceType,
    resource_id: int,
    owner_id: int | None,
    user_id: int,
) -> None:
    if not can_edit(resource_type, resource_id, owner_id, user_id):
        raise NotAuthorized("You do not have permission to edit this resource.")


def grant(
    resource_type: ResourceType,
    resource_id: int,
    *,
    user_id: int,
    level: PermissionLevel,
    granted_by_id: int | None,
) -> None:
    """Create or update a share. Only the owner should be able to call this."""
    session = get_session_factory()()
    try:
        row = session.scalar(
            select(ResourcePermission).where(
                ResourcePermission.resource_type == resource_type,
                ResourcePermission.resource_id == resource_id,
                ResourcePermission.user_id == user_id,
            )
        )
        if row is None:
            row = ResourcePermission(
                resource_type=resource_type,
                resource_id=resource_id,
                user_id=user_id,
                level=level,
                granted_by_id=granted_by_id,
            )
            session.add(row)
        else:
            row.level = level
            row.granted_by_id = granted_by_id
        session.commit()
    finally:
        session.close()


def revoke(resource_type: ResourceType, resource_id: int, *, user_id: int) -> bool:
    session = get_session_factory()()
    try:
        row = session.scalar(
            select(ResourcePermission).where(
                ResourcePermission.resource_type == resource_type,
                ResourcePermission.resource_id == resource_id,
                ResourcePermission.user_id == user_id,
            )
        )
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
    finally:
        session.close()


def revoke_all(resource_type: ResourceType, resource_id: int) -> int:
    """Delete every grant for a resource. Returns how many were removed.
    Used when a resource is switched back to private. Deleting the grants rather
    than only flagging the resource keeps "who may read this" answerable from the
    grants alone: a lingering grant can never silently come back to life just
    because the visibility flag flipped again.
    """
    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(ResourcePermission).where(
                    ResourcePermission.resource_type == resource_type,
                    ResourcePermission.resource_id == resource_id,
                )
            )
        )
        for row in rows:
            session.delete(row)
        session.commit()
        return len(rows)
    finally:
        session.close()


def shares_for(resource_type: ResourceType, resource_id: int) -> list[ResourcePermission]:
    session = get_session_factory()()
    try:
        return list(
            session.scalars(
                select(ResourcePermission).where(
                    ResourcePermission.resource_type == resource_type,
                    ResourcePermission.resource_id == resource_id,
                )
            )
        )
    finally:
        session.close()
