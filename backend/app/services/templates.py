"""Template CRUD, immutable versioning and sharing.

Design rules this module enforces:

* **A version is immutable.** Saving never edits a version row; it appends a new
  one and copies the step rows forward. Deletion is the only thing that removes
  version rows, and it is blocked while an execution still references them.
* **A no-op save creates no version.** The content checksum covers metadata and
  steps, so re-saving an unchanged template leaves the version number alone
  instead of inflating history.
* **Writes require the edit lock and a fresh ``row_version``.** Both are checked
  inside the same transaction as the write, so a concurrent editor is rejected
  rather than silently overwritten.
* **Ownership is read from the row, never from the request.** An editor with an
  ``edit`` share can change content but cannot re-share, re-own, or delete.

Every function here takes a ``Principal`` rather than a bare user id so audit
records are attributable; the HTTP layer never assembles authorization itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.session import get_session_factory
from app.db.types import utcnow
from app.models.audit_actions import AuditAction
from app.models.enums import RunAsMethod
from app.models.permission import (
    PermissionLevel,
    ResourceLock,
    ResourcePermission,
    ResourceType,
)
from app.models.template import (
    Template,
    TemplateStep,
    TemplateVersion,
    TemplateVisibility,
)
from app.models.user import User
from app.services import audit, locks, permissions
from app.services.conditions import ConditionError, StepRule, validate_rule
from app.services.sessions import Principal

logger = logging.getLogger(__name__)

RESOURCE = ResourceType.TEMPLATE

MAX_STEPS = 100
MAX_SCRIPT_BYTES = 200_000


class TemplateNotFound(LookupError):
    """No such template, or the caller may not see it."""


class TemplateConflict(RuntimeError):
    """Name already used, or the template is referenced by a workflow."""


@dataclass(frozen=True)
class StepData:
    """Validated step input, independent of the transport layer."""

    name: str
    script: str
    description: str | None = None
    interpreter: str = "bash"
    timeout_seconds: int = 300
    rule: StepRule = field(default_factory=StepRule)
    run_as_username: str | None = None
    run_as_method: RunAsMethod | None = None


@dataclass(frozen=True)
class TemplateBody:
    """The editable part of a template."""

    name: str
    description: str | None = None
    steps: tuple[StepData, ...] = field(default_factory=tuple)

    def steps_as_dicts(self) -> list[dict[str, object]]:
        """Canonical payload for ``compute_content_checksum``.

        ``rule`` and the Run-As pair are added only when a step actually uses
        them, so the digest of a template that never did is unchanged from the
        previous milestone -- an upgrade must not make a no-op save append a
        version.
        """
        payload: list[dict[str, object]] = []
        for step in self.steps:
            entry: dict[str, object] = {
                "name": step.name,
                "description": step.description,
                "script": step.script,
                "interpreter": step.interpreter,
                "timeout_seconds": step.timeout_seconds,
            }
            rule = step.rule.as_dict()
            if rule is not None:
                entry["rule"] = rule
            if step.run_as_username is not None:
                entry["run_as_username"] = step.run_as_username
                entry["run_as_method"] = (
                    step.run_as_method.value if step.run_as_method is not None else None
                )
            payload.append(entry)
        return payload


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def _visible_filter(user_id: int):
    """SQL for "templates I own, or that were explicitly granted to me".

    Grants alone decide access (mirroring ``permissions.can_view``), so this must
    not consult ``visibility``: doing so would make the list disagree with the
    detail route, hiding a template the caller can open -- or advertising one that
    answers 404. Sharing is revoked by deleting grant rows, not by a flag.
    """
    shared_ids = select(ResourcePermission.resource_id).where(
        ResourcePermission.resource_type == RESOURCE,
        ResourcePermission.user_id == user_id,
    )
    return or_(Template.owner_id == user_id, Template.id.in_(shared_ids))


def _load(session: Session, template_id: int) -> Template:
    template = session.scalars(
        select(Template)
        .where(Template.id == template_id)
        .options(selectinload(Template.versions).selectinload(TemplateVersion.steps))
    ).first()
    if template is None:
        raise TemplateNotFound(template_id)
    return template


def get(template_id: int, principal: Principal) -> Template:
    """Load a template the caller may view, or raise ``TemplateNotFound``.

    An unauthorized caller gets the same error as a missing row so the response
    cannot be used to probe which template ids exist.
    """
    session = get_session_factory()()
    try:
        template = _load(session, template_id)
        if not permissions.can_view(RESOURCE, template.id, template.owner_id, principal.user_id):
            raise TemplateNotFound(template_id)
        return template
    finally:
        session.close()


def can_edit(template: Template, principal: Principal) -> bool:
    return permissions.can_edit(RESOURCE, template.id, template.owner_id, principal.user_id)


def is_owner(template: Template, principal: Principal) -> bool:
    return template.owner_id == principal.user_id


def list_for_user(
    principal: Principal,
    *,
    search: str | None = None,
    owned_only: bool = False,
    limit: int = 200,
) -> list[Template]:
    """Templates visible to the caller, newest edit first."""
    session = get_session_factory()()
    try:
        query = select(Template).options(
            selectinload(Template.versions).selectinload(TemplateVersion.steps)
        )
        if owned_only:
            query = query.where(Template.owner_id == principal.user_id)
        else:
            query = query.where(_visible_filter(principal.user_id))
        if search:
            needle = f"%{search.strip().lower()}%"
            query = query.where(
                or_(
                    func.lower(Template.name).like(needle),
                    func.lower(func.coalesce(Template.description, "")).like(needle),
                )
            )
        query = query.order_by(Template.updated_at.desc(), Template.id.desc()).limit(limit)
        return list(session.scalars(query))
    finally:
        session.close()


def owner_labels(template: Template) -> str | None:
    session = get_session_factory()()
    try:
        user = session.get(User, template.owner_id) if template.owner_id else None
        return user.label if user else None
    finally:
        session.close()


def shares(template_id: int) -> list[ResourcePermission]:
    return permissions.shares_for(RESOURCE, template_id)


def version_history(template_id: int, principal: Principal) -> list[TemplateVersion]:
    template = get(template_id, principal)
    return list(template.versions)


def version(template_id: int, version_number: int, principal: Principal) -> TemplateVersion:
    template = get(template_id, principal)
    for candidate in template.versions:
        if candidate.version == version_number:
            return candidate
    raise TemplateNotFound(f"version {version_number}")


# --------------------------------------------------------------------------- #
# Validation helpers (shared with the HTTP layer)
# --------------------------------------------------------------------------- #


def validate_body(body: TemplateBody) -> None:
    """Reject inputs that would be stored but never usable.

    The interpreter rule is duplicated from the execution schema on purpose:
    this value ends up in a remote command line, and the template editor is a
    second door into the same field.
    """
    if not body.name.strip():
        raise ValueError("name must not be blank")
    if len(body.name) > 120:
        raise ValueError("name must be at most 120 characters")
    if len(body.steps) > MAX_STEPS:
        raise ValueError(f"a template may have at most {MAX_STEPS} steps")

    for index, step in enumerate(body.steps, start=1):
        if not step.name.strip():
            raise ValueError(f"step {index}: name must not be blank")
        if not step.script.strip():
            raise ValueError(f"step {index}: script must not be empty")
        if len(step.script.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise ValueError(f"step {index}: script is too large")
        if not 1 <= step.timeout_seconds <= 86400:
            raise ValueError(f"step {index}: timeout must be between 1 and 86400 seconds")
        cleaned = step.interpreter.strip()
        if not cleaned or not cleaned.replace("-", "").replace(".", "").isalnum():
            raise ValueError(
                f"step {index}: interpreter must be a single program name, e.g. 'bash'"
            )
        try:
            validate_rule(step.rule)
        except ConditionError as exc:
            # Re-raised with the step number so the editor can point at it.
            raise ValueError(f"step {index}: {exc}") from exc


def _next_version_number(session: Session, template_id: int) -> int:
    current = session.scalar(
        select(func.max(TemplateVersion.version)).where(TemplateVersion.template_id == template_id)
    )
    return (current or 0) + 1


def _append_version(
    session: Session, template: Template, body: TemplateBody, *, actor: Principal
) -> TemplateVersion:
    """Write a new immutable version with its step rows."""
    snapshot = TemplateVersion(
        template_id=template.id,
        version=_next_version_number(session, template.id),
        name=body.name,
        description=body.description,
        content_checksum=TemplateVersion.compute_content_checksum(
            name=body.name, description=body.description, steps=body.steps_as_dicts()
        ),
        created_by_id=actor.user_id,
    )
    session.add(snapshot)
    session.flush()
    for position, step in enumerate(body.steps):
        row = TemplateStep(
            version_id=snapshot.id,
            position=position,
            name=step.name,
            description=step.description,
            script=step.script,
            interpreter=step.interpreter.strip(),
            timeout_seconds=step.timeout_seconds,
            run_as_username=step.run_as_username,
            run_as_method=step.run_as_method,
        )
        row.set_rule(step.rule)
        session.add(row)
    session.flush()
    return snapshot


def _body_from(template: Template, latest: TemplateVersion | None) -> TemplateBody:
    if latest is None:
        return TemplateBody(name=template.name, description=template.description, steps=())
    return TemplateBody(
        name=template.name,
        description=template.description,
        steps=tuple(
            StepData(
                name=step.name,
                description=step.description,
                script=step.script,
                interpreter=step.interpreter,
                timeout_seconds=step.timeout_seconds,
                rule=step.to_rule(),
                run_as_username=step.run_as_username,
                run_as_method=step.run_as_method,
            )
            for step in latest.steps
        ),
    )


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def create(
    body: TemplateBody,
    *,
    principal: Principal,
    visibility: TemplateVisibility = TemplateVisibility.PRIVATE,
    remote_addr: str | None = None,
) -> Template:
    """Create a template and its first version.

    A template with no steps is allowed: the editor needs somewhere to save a
    draft before the first step is written. ``validate_body`` still rejects blank
    names and malformed steps.
    """
    validate_body(body)
    session = get_session_factory()()
    try:
        duplicate = session.scalar(
            select(Template).where(
                Template.owner_id == principal.user_id,
                func.lower(Template.name) == body.name.strip().lower(),
            )
        )
        if duplicate is not None:
            raise TemplateConflict(f"You already have a template named {body.name!r}.")

        template = Template(
            owner_id=principal.user_id,
            name=body.name.strip(),
            description=body.description,
            visibility=visibility,
            row_version=1,
        )
        session.add(template)
        session.flush()

        snapshot = _append_version(session, template, body, actor=principal)
        session.commit()
        template_id = template.id
        version_number = snapshot.version
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    audit.record(
        AuditAction.TEMPLATE_CREATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=template_id,
        remote_addr=remote_addr,
        detail={"version": version_number, "step_count": len(body.steps)},
    )
    audit.record(
        AuditAction.TEMPLATE_VERSION_CREATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=template_id,
        remote_addr=remote_addr,
        detail={"version": version_number},
    )
    return get(template_id, principal)


def update(
    template_id: int,
    body: TemplateBody,
    *,
    expected_row_version: int | None,
    principal: Principal,
    remote_addr: str | None = None,
) -> tuple[Template, bool]:
    """Replace a template's editable content, appending a version if it changed.

    Returns ``(template, version_created)``. The lock is asserted and the row
    version checked inside the same session as the write, so the read that
    decides "did anything change?" cannot go stale under us.
    """
    validate_body(body)
    session = get_session_factory()()
    try:
        template = _load(session, template_id)
        if not permissions.can_edit(RESOURCE, template.id, template.owner_id, principal.user_id):
            raise TemplateNotFound(template_id)

        locks.assert_holds(
            RESOURCE,
            template_id,
            user_id=principal.user_id,
            session_id=principal.session_id or 0,
        )
        locks.check_version(expected_row_version, template.row_version)

        if body.name.strip().lower() != template.name.lower():
            clash = session.scalar(
                select(Template).where(
                    Template.owner_id == template.owner_id,
                    func.lower(Template.name) == body.name.strip().lower(),
                    Template.id != template_id,
                )
            )
            if clash is not None:
                raise TemplateConflict(f"You already have a template named {body.name!r}.")

        latest = template.latest_version
        previous = _body_from(template, latest)
        changed = (
            previous.name != body.name.strip()
            or (previous.description or None) != (body.description or None)
            or previous.steps != body.steps
        )

        template.name = body.name.strip()
        template.description = body.description
        template.row_version += 1

        new_version: TemplateVersion | None = None
        if changed:
            new_version = _append_version(session, template, body, actor=principal)

        session.commit()
        version_number = new_version.version if new_version else None
        row_version = template.row_version
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    if new_version is not None:
        audit.record(
            AuditAction.TEMPLATE_VERSION_CREATED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            resource_type=RESOURCE.value,
            resource_id=template_id,
            remote_addr=remote_addr,
            detail={"version": version_number, "step_count": len(body.steps)},
        )
    audit.record(
        AuditAction.TEMPLATE_UPDATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=template_id,
        remote_addr=remote_addr,
        detail={
            "row_version": row_version,
            "version": version_number,
            "version_created": new_version is not None,
            "step_count": len(body.steps),
        },
    )
    return get(template_id, principal), new_version is not None


def set_visibility(
    template_id: int,
    visibility: TemplateVisibility,
    *,
    principal: Principal,
    remote_addr: str | None = None,
) -> Template:
    """Owner-only. Making a template private also drops every grant.
    Deleting the grants here (rather than relying on a visibility flag at read
    time) keeps grants as the single source of truth for access. Sharing the same
    template again therefore starts from an empty list instead of silently
    restoring people who were cut off earlier.
    """
    session = get_session_factory()()
    try:
        template = _load(session, template_id)
        if template.owner_id != principal.user_id:
            raise permissions.NotAuthorized("Only the owner can change sharing settings.")
        previous = template.visibility
        template.visibility = visibility
        template.row_version += 1
        session.commit()
        row_version = template.row_version
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    revoked = 0
    if visibility is TemplateVisibility.PRIVATE:
        revoked = permissions.revoke_all(RESOURCE, template_id)
        _release_all_locks(template_id)

    audit.record(
        AuditAction.TEMPLATE_VISIBILITY_CHANGED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=template_id,
        remote_addr=remote_addr,
        detail={
            "from": previous.value,
            "to": visibility.value,
            "row_version": row_version,
            "grants_revoked": revoked,
        },
    )
    return get(template_id, principal)


def share(
    template_id: int,
    *,
    target_user_id: int,
    level: PermissionLevel,
    principal: Principal,
    remote_addr: str | None = None,
) -> None:
    """Owner-only grant. Sharing with yourself is refused as meaningless."""
    session = get_session_factory()()
    try:
        template = _load(session, template_id)
        if template.owner_id != principal.user_id:
            raise permissions.NotAuthorized("Only the owner can share this template.")
        if target_user_id == principal.user_id:
            raise ValueError("A template is already yours; sharing with yourself is a no-op.")
        target = session.get(User, target_user_id)
        if target is None:
            raise TemplateNotFound(target_user_id)
        template_label = target.label
    finally:
        session.close()

    permissions.grant(
        RESOURCE,
        template_id,
        user_id=target_user_id,
        level=level,
        granted_by_id=principal.user_id,
    )
    audit.record(
        AuditAction.TEMPLATE_SHARED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=template_id,
        remote_addr=remote_addr,
        detail={
            "shared_with": template_label,
            "shared_with_user_id": target_user_id,
            "level": level.value,
        },
    )


def unshare(
    template_id: int,
    *,
    target_user_id: int,
    principal: Principal,
    remote_addr: str | None = None,
) -> bool:
    """Owner-only revoke. Also releases any lock the removed editor still holds."""
    session = get_session_factory()()
    try:
        template = _load(session, template_id)
        if template.owner_id != principal.user_id:
            raise permissions.NotAuthorized("Only the owner can revoke sharing.")
    finally:
        session.close()

    removed = permissions.revoke(RESOURCE, template_id, user_id=target_user_id)
    if removed:
        _release_locks_for_user(template_id, target_user_id)
        audit.record(
            AuditAction.TEMPLATE_UNSHARED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            resource_type=RESOURCE.value,
            resource_id=template_id,
            remote_addr=remote_addr,
            detail={"revoked_user_id": target_user_id},
        )
    return removed


def _release_locks_for_user(template_id: int, user_id: int) -> int:
    """Drop a user's lock on a template. Used when their access is revoked.

    Without this, losing an ``edit`` share would still leave the lock held until
    it expired, blocking the owner for up to the full lease.
    """
    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(ResourceLock).where(
                    ResourceLock.resource_type == RESOURCE,
                    ResourceLock.resource_id == template_id,
                    ResourceLock.user_id == user_id,
                    ResourceLock.released_at.is_(None),
                )
            )
        )
        for row in rows:
            row.released_at = utcnow()
            row.released_reason = "access_revoked"
        session.commit()
        return len(rows)
    finally:
        session.close()


def duplicate(
    template_id: int,
    *,
    principal: Principal,
    name: str | None = None,
    remote_addr: str | None = None,
) -> Template:
    """Copy a template the caller can view into their own namespace.

    The copy is always private and always owned by the caller, even when the
    source was a shared template owned by somebody else: duplicating must not
    inherit the original's sharing or write access.
    """
    source = get(template_id, principal)
    latest = source.latest_version
    base_name = (name or f"{source.name} (copy)").strip()[:120]
    body = TemplateBody(
        name=base_name,
        description=source.description,
        steps=tuple(
            StepData(
                name=step.name,
                description=step.description,
                script=step.script,
                interpreter=step.interpreter,
                timeout_seconds=step.timeout_seconds,
                rule=step.to_rule(),
            )
            for step in (latest.steps if latest else [])
        ),
    )

    session = get_session_factory()()
    try:
        suffix = 2
        candidate = base_name
        while session.scalar(
            select(Template.id).where(
                Template.owner_id == principal.user_id,
                func.lower(Template.name) == candidate.lower(),
            )
        ):
            tail = f" ({suffix})"
            candidate = f"{base_name[: 120 - len(tail)]}{tail}"
            suffix += 1
            if suffix > 100:  # pragma: no cover - absurd name churn
                raise TemplateConflict("Could not find a free name for the copy.")
    finally:
        session.close()

    body = TemplateBody(name=candidate, description=body.description, steps=body.steps)
    created = create(
        body, principal=principal, visibility=TemplateVisibility.PRIVATE, remote_addr=remote_addr
    )
    audit.record(
        AuditAction.TEMPLATE_DUPLICATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=created.id,
        remote_addr=remote_addr,
        detail={
            "source_template_id": template_id,
            "source_version": latest.version if latest else None,
        },
    )
    return created


def references_for(template_id: int) -> dict[str, int]:
    """What would break if this template were deleted.

    Two kinds of reference exist today:

    * ``workflow_steps`` pin a ``template_version_id`` with ``ON DELETE RESTRICT``,
      so a workflow would lose its pinned content. That must block deletion.
    * ``execution_template_snapshots`` keep their own copy of the script and store
      the ids with ``ON DELETE SET NULL``, so history survives untouched. Those
      are reported for information only.
    """
    from app.models.execution import ExecutionTemplateSnapshot
    from app.models.workflow import WorkflowStep

    session = get_session_factory()()
    try:
        version_ids = select(TemplateVersion.id).where(TemplateVersion.template_id == template_id)
        workflow_refs = session.scalar(
            select(func.count())
            .select_from(WorkflowStep)
            .where(WorkflowStep.template_version_id.in_(version_ids))
        )
        snapshot_refs = session.scalar(
            select(func.count())
            .select_from(ExecutionTemplateSnapshot)
            .where(ExecutionTemplateSnapshot.template_id == template_id)
        )
        return {"workflow_steps": workflow_refs or 0, "execution_snapshots": snapshot_refs or 0}
    finally:
        session.close()


def delete(
    template_id: int,
    *,
    principal: Principal,
    remote_addr: str | None = None,
) -> None:
    """Owner-only delete, refused while a workflow pins one of its versions.

    Execution history is *not* a blocker: snapshots carry their own script copy,
    so removing the template preserves what already ran. The audit record is
    written after the delete so ``resource_id`` deliberately refers to a row that
    no longer exists -- that is the point of an append-only trail.
    """
    refs = references_for(template_id)
    session = get_session_factory()()
    try:
        template = _load(session, template_id)
        if template.owner_id != principal.user_id:
            # A shared editor must not be able to delete the owner's template,
            # and 404 avoids confirming the id to an outsider.
            raise TemplateNotFound(template_id)
        if refs["workflow_steps"]:
            raise TemplateConflict(
                "This template is used by a workflow. Remove it from the workflow "
                "before deleting it."
            )
        name = template.name
        versions = len(template.versions)
        session.delete(template)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _release_all_locks(template_id)
    audit.record(
        AuditAction.TEMPLATE_DELETED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type=RESOURCE.value,
        resource_id=template_id,
        remote_addr=remote_addr,
        detail={
            "name": name,
            "versions_deleted": versions,
            "execution_snapshots_retained": refs["execution_snapshots"],
        },
    )


def _release_all_locks(template_id: int) -> int:
    """Drop every lock on a deleted template so the ids can be reused cleanly."""
    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(ResourceLock).where(
                    ResourceLock.resource_type == RESOURCE,
                    ResourceLock.resource_id == template_id,
                    ResourceLock.released_at.is_(None),
                )
            )
        )
        for row in rows:
            row.released_at = utcnow()
            row.released_reason = "resource_deleted"
        session.commit()
        return len(rows)
    finally:
        session.close()
