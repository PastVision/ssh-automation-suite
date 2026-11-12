"""Minimal ownership and sharing authorization.

Deliberately not a role framework. Two rules:

* A resource belongs to exactly one owner. The owner always has full access.
* Another user needs an explicit ``ResourcePermission`` row, with ``view`` or
  ``edit``.
* A resource whose ``visibility`` is ``public`` is additionally viewable by any
  *authenticated* user. Public never grants ``edit`` and never grants execution.

"Public" here means "visible to other authenticated users of this application".
It is not an anonymous/internet exposure: every route and every WebSocket still
requires a session, so there is no code path where an unauthenticated request
reaches a public resource.

Execution rights are separate from viewing on purpose. Publishing a workflow
definition must never let every user start SSH work against its hosts, so
``can_run_workflow`` consults ``execution_permission`` (and, for ``selected``,
the same grant table) rather than visibility.

Credentials and SSH profiles are *not* shared resources: they are private to their
owner with no sharing path at all, so those checks are ownership-only.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.enums import LogVisibility
from app.models.permission import PermissionLevel, ResourcePermission, ResourceType
from app.models.workflow import ExecutionPermission

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


# Access is decided by grants (plus public visibility) alone. There is
# deliberately no separate visibility policy switch here: a second policy living in
# this module is how the list query and the detail check drifted apart before.


def _is_public(visibility: object) -> bool:
    """True when ``visibility`` is the public level of whatever enum it belongs to.

    Compared by value rather than by type so ``TemplateVisibility``,
    ``WorkflowVisibility`` and any future equivalent share one rule instead of
    three near-identical ones that could drift.
    """
    return getattr(visibility, "value", visibility) == "public"


def can_view(
    resource_type: ResourceType,
    resource_id: int,
    owner_id: int | None,
    user_id: int,
    *,
    visibility: object | None = None,
) -> bool:
    """Whether ``user_id`` may read the resource.

    ``visibility`` is opt-in so existing callers that predate sharing keep their
    exact behaviour (owner or explicit grant). A public resource widens the answer
    to any authenticated user, which is what makes public discovery work without a
    grant row per viewer.
    """
    if owner_id is not None and owner_id == user_id:
        return True
    if _is_public(visibility):
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


def can_run_workflow(
    *,
    workflow_id: int,
    owner_id: int | None,
    user_id: int,
    execution_permission: ExecutionPermission,
) -> bool:
    """Whether ``user_id`` may start a run of this workflow.

    ``ALL_AUTHENTICATED`` is intentionally the only setting that ignores grants:
    it is the one an owner chose explicitly, and even then the caller is already
    authenticated (every route requires a session).
    """
    if owner_id is not None and owner_id == user_id:
        return True
    if execution_permission is ExecutionPermission.ALL_AUTHENTICATED:
        return True
    if execution_permission is ExecutionPermission.SELECTED:
        # Any grant -- view or edit -- counts as "selected", because the owner
        # picked this user. Enumerating a second, separate execution allowlist
        # would duplicate the sharing UI for no additional safety.
        return permission_for(ResourceType.WORKFLOW, workflow_id, user_id) is not None
    return False


def can_read_logs(
    *,
    owner_id: int | None,
    user_id: int,
    log_visibility: LogVisibility,
    workflow_id: int | None,
) -> bool:
    """Whether ``user_id`` may read an execution's transcripts.

    ``SHARED`` means "whoever can already see the workflow", which is why it
    consults the workflow grant rather than the execution's own (executions have
    no grant table of their own). A public log is readable by any authenticated
    user, independent of the workflow's visibility -- and the reverse also holds,
    so making a workflow public does not retroactively publish its logs.
    """
    if owner_id is not None and owner_id == user_id:
        return True
    if log_visibility is LogVisibility.PUBLIC:
        return True
    if log_visibility is LogVisibility.SHARED and workflow_id is not None:
        return permission_for(ResourceType.WORKFLOW, workflow_id, user_id) is not None
    return False


def require_view(
    resource_type: ResourceType,
    resource_id: int,
    owner_id: int | None,
    user_id: int,
    *,
    visibility: object | None = None,
) -> None:
    if not can_view(
        resource_type, resource_id, owner_id, user_id, visibility=visibility
    ):
        raise NotAuthorized("You do not have access to this resource.")


def require_edit(
    resource_type: ResourceType,
    resource_id: int,
    owner_id: int | None,
    user_id: int,
) -> None:
    if not can_edit(resource_type, resource_id, owner_id, user_id):
        raise NotAuthorized("You do not have permission to edit this resource.")


def require_run(
    *,
    workflow_id: int,
    owner_id: int | None,
    user_id: int,
    execution_permission: ExecutionPermission,
) -> None:
    if not can_run_workflow(
        workflow_id=workflow_id,
        owner_id=owner_id,
        user_id=user_id,
        execution_permission=execution_permission,
    ):
        raise NotAuthorized(
            "You have permission to view this workflow but not to run it."
        )


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
