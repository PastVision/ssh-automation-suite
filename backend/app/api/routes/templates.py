"""Template endpoints.

Authorization lives in ``app.services.templates``; this module only translates
between HTTP and that service, so there is exactly one place where "can this user
do that?" is decided.

Two conventions worth stating:

* A template the caller cannot see answers **404**, not 403. Confirming that an id
  exists is itself a small disclosure and the UI has no use for it.
* A write that arrives without the edit lock answers **423 Locked**, and one that
  arrives with a stale ``row_version`` answers **409**. They are different
  situations for the client: the first means "start editing first", the second
  means "reload, somebody saved".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.db.session import get_session_factory
from app.models.audit_actions import AuditAction, AuditOutcome
from app.models.permission import PermissionLevel, ResourceType
from app.models.template import Template
from app.models.user import User
from app.schemas.locks import LockRead
from app.schemas.template import (
    ShareGrant,
    ShareRead,
    StepConditionRead,
    StepRuleInput,
    StepRuleRead,
    TemplateCreate,
    TemplateDetail,
    TemplateDuplicate,
    TemplateListResponse,
    TemplateStepInput,
    TemplateStepRead,
    TemplateSummary,
    TemplateUpdate,
    TemplateVersionRead,
    TemplateVersionSummary,
    TemplateVisibilityUpdate,
)
from app.services import audit, conditions, locks, permissions, templates
from app.services.identity import client_ip, require_principal
from app.services.sessions import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["templates"])


# --------------------------------------------------------------------------- #
# Translation helpers
# --------------------------------------------------------------------------- #

def _steps_from(payload: list[TemplateStepInput]) -> tuple[templates.StepData, ...]:
    return tuple(
        templates.StepData(
            name=item.name.strip(),
            description=item.description,
            script=item.script,
            interpreter=item.interpreter,
            timeout_seconds=item.timeout_seconds,
            rule=_rule_from(item.rule),
            run_as_username=item.run_as_username,
            run_as_method=item.run_as_method,
        )
        for item in payload
    )


def _rule_from(payload: StepRuleInput) -> conditions.StepRule:
    """Translate the wire rule into the service-layer rule.

    The schema has already enforced the required/forbidden combinations, so this
    is a straight mapping; ``services.conditions.validate_rule`` still runs at save
    time and is the single authority for what a coherent rule is.
    """
    if payload.condition is None:
        return conditions.StepRule(action=payload.action)
    return conditions.StepRule(
        action=payload.action,
        condition=conditions.Condition(
            stream=payload.condition.stream,
            location=payload.condition.location,
            operator=payload.condition.operator,
            expected=payload.condition.expected,
            case_sensitive=payload.condition.case_sensitive,
            line_number=payload.condition.line_number,
            anchor=payload.condition.anchor,
        ),
    )


def _rule_read(step) -> StepRuleRead:
    """Render a stored step's rule; unconditional steps carry no condition."""
    rule = step.to_rule()
    if rule.condition is None:
        return StepRuleRead(action=rule.action)
    condition = rule.condition
    return StepRuleRead(
        action=rule.action,
        condition=StepConditionRead(
            stream=condition.stream,
            location=condition.location,
            operator=condition.operator,
            expected=condition.expected,
            case_sensitive=condition.case_sensitive,
            line_number=condition.line_number,
            anchor=condition.anchor,
        ),
    )


def _lock_read(info: locks.LockInfo | None) -> LockRead | None:
    if info is None:
        return None
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


def _audit_denied(
    principal: Principal, request: Request, template_id: int | None, reason: str
) -> None:
    audit.record(
        AuditAction.ACCESS_DENIED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        outcome=AuditOutcome.DENIED,
        resource_type=ResourceType.TEMPLATE.value,
        resource_id=template_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={"reason": reason},
    )


def _summary_fields(
    template: Template, principal: Principal
) -> dict[str, object]:
    latest = template.latest_version
    holder = locks.current_holder(
        ResourceType.TEMPLATE, template.id, user_id=principal.user_id
    )
    return {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "owner_id": template.owner_id,
        "owner_label": templates.owner_labels(template),
        "visibility": template.visibility,
        "is_owner": template.owner_id == principal.user_id,
        "can_edit": templates.can_edit(template, principal),
        "row_version": template.row_version,
        "current_version": latest.version if latest else None,
        "step_count": len(latest.steps) if latest else 0,
        "lock": _lock_read(holder),
        "locked_by_other": holder is not None and not holder.is_mine,
        "updated_at": template.updated_at,
        "created_at": template.created_at,
    }


def _detail(template: Template, principal: Principal) -> TemplateDetail:
    latest = template.latest_version
    shares = (
        templates.shares(template.id)
        if template.owner_id == principal.user_id
        else []
    )
    return TemplateDetail(
        **_summary_fields(template, principal),
        steps=[
            TemplateStepRead(
                position=step.position,
                name=step.name,
                description=step.description,
                script=step.script,
                interpreter=step.interpreter,
                timeout_seconds=step.timeout_seconds,
                rule=_rule_read(step),
                run_as_username=step.run_as_username,
                run_as_method=step.run_as_method,
            )
            for step in (latest.steps if latest else [])
        ],
        versions=[
            TemplateVersionSummary(
                id=version.id,
                version=version.version,
                name=version.name,
                created_at=version.created_at,
                step_count=len(version.steps),
            )
            for version in reversed(template.versions)
        ],
        shares=[_share_read(row) for row in shares],
    )


def _share_read(row) -> ShareRead:
    session = get_session_factory()()
    try:
        user = session.get(User, row.user_id)
        return ShareRead(
            user_id=row.user_id,
            username=user.username if user else "unknown",
            display_name=user.display_name if user else None,
            level=row.level.value,
            granted_by_id=row.granted_by_id,
            created_at=row.created_at,
        )
    finally:
        session.close()


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Template not found"
    )


def _locked(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_423_LOCKED, detail=detail)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

@router.get("", response_model=TemplateListResponse, summary="List templates I can see")
def list_templates(
    search: str | None = Query(default=None, max_length=120),
    owned: bool = Query(default=False, description="Only templates I own"),
    principal: Principal = Depends(require_principal),
) -> TemplateListResponse:
    rows = templates.list_for_user(principal, search=search, owned_only=owned)
    return TemplateListResponse(
        templates=[TemplateSummary(**_summary_fields(row, principal)) for row in rows],
        total=len(rows),
    )


@router.post(
    "",
    response_model=TemplateDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a template",
)
def create_template(
    payload: TemplateCreate,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> TemplateDetail:
    body = templates.TemplateBody(
        name=payload.name.strip(),
        description=payload.description,
        steps=_steps_from(payload.steps),
    )
    try:
        template = templates.create(
            body,
            principal=principal,
            visibility=payload.visibility,
            remote_addr=client_ip(request),
        )
    except templates.TemplateConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _detail(template, principal)


@router.get("/{template_id}", response_model=TemplateDetail, summary="Read a template")
def read_template(
    template_id: int,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> TemplateDetail:
    try:
        template = templates.get(template_id, principal)
    except templates.TemplateNotFound as exc:
        _audit_denied(principal, request, template_id, "template_not_visible")
        raise _not_found() from exc
    return _detail(template, principal)


@router.put(
    "/{template_id}",
    response_model=TemplateDetail,
    summary="Replace template content",
    responses={
        409: {"description": "Stale row_version or duplicate name"},
        423: {"description": "The edit lock is not held by this session"},
    },
)
def update_template(
    template_id: int,
    payload: TemplateUpdate,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> TemplateDetail:
    body = templates.TemplateBody(
        name=payload.name.strip(),
        description=payload.description,
        steps=_steps_from(payload.steps),
    )
    try:
        template, _created = templates.update(
            template_id,
            body,
            expected_row_version=payload.row_version,
            principal=principal,
            remote_addr=client_ip(request),
        )
    except templates.TemplateNotFound as exc:
        _audit_denied(principal, request, template_id, "template_not_editable")
        raise _not_found() from exc
    except locks.LockRequired as exc:
        audit.record(
            AuditAction.LOCK_STOLEN,
            actor_id=principal.user_id,
            actor_username=principal.label,
            outcome=AuditOutcome.DENIED,
            resource_type=ResourceType.TEMPLATE.value,
            resource_id=template_id,
            session_id=principal.session_id,
            remote_addr=client_ip(request),
            detail={"reason": "edit_without_lock"},
        )
        raise _locked(str(exc)) from exc
    except locks.LockConflict as exc:
        _audit_denied(principal, request, template_id, "edit_lock_held_by_other")
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "message": f"{exc.holder.holder_label} is currently editing this template.",
                "lock": _lock_read(exc.holder).model_dump(mode="json"),
            },
        ) from exc
    except locks.StaleVersion as exc:
        audit.record(
            AuditAction.LOCK_STOLEN,
            actor_id=principal.user_id,
            actor_username=principal.label,
            outcome=AuditOutcome.DENIED,
            resource_type=ResourceType.TEMPLATE.value,
            resource_id=template_id,
            session_id=principal.session_id,
            remote_addr=client_ip(request),
            detail={"reason": "stale_row_version", "current": exc.current_version},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "This template changed since you loaded it. Reload before saving."
                ),
                "current_row_version": exc.current_version,
            },
        ) from exc
    except templates.TemplateConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _detail(template, principal)


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a template I own",
    responses={409: {"description": "A workflow still uses this template"}},
)
def delete_template(
    template_id: int,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Response:
    try:
        templates.delete(template_id, principal=principal, remote_addr=client_ip(request))
    except templates.TemplateNotFound as exc:
        _audit_denied(principal, request, template_id, "template_not_deletable")
        raise _not_found() from exc
    except templates.TemplateConflict as exc:
        refs = templates.references_for(template_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "workflow_steps": refs["workflow_steps"],
                "execution_snapshots": refs["execution_snapshots"],
            },
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{template_id}/duplicate",
    response_model=TemplateDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Copy a template into my own namespace",
)
def duplicate_template(
    template_id: int,
    request: Request,
    payload: TemplateDuplicate | None = None,
    principal: Principal = Depends(require_principal),
) -> TemplateDetail:
    try:
        copy = templates.duplicate(
            template_id,
            principal=principal,
            name=payload.name if payload else None,
            remote_addr=client_ip(request),
        )
    except templates.TemplateNotFound as exc:
        _audit_denied(principal, request, template_id, "template_not_duplicable")
        raise _not_found() from exc
    except templates.TemplateConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _detail(copy, principal)


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #

@router.get(
    "/{template_id}/versions",
    response_model=list[TemplateVersionSummary],
    summary="Version history, newest first",
)
def list_versions(
    template_id: int,
    principal: Principal = Depends(require_principal),
) -> list[TemplateVersionSummary]:
    try:
        history = templates.version_history(template_id, principal)
    except templates.TemplateNotFound as exc:
        raise _not_found() from exc
    return [
        TemplateVersionSummary(
            id=version.id,
            version=version.version,
            name=version.name,
            created_at=version.created_at,
            step_count=len(version.steps),
        )
        for version in reversed(history)
    ]


@router.get(
    "/{template_id}/versions/{version_number}",
    response_model=TemplateVersionRead,
    summary="One immutable version, with its steps",
)
def read_version(
    template_id: int,
    version_number: int,
    principal: Principal = Depends(require_principal),
) -> TemplateVersionRead:
    try:
        version = templates.version(template_id, version_number, principal)
    except templates.TemplateNotFound as exc:
        raise _not_found() from exc
    return TemplateVersionRead(
        id=version.id,
        version=version.version,
        name=version.name,
        description=version.description,
        content_checksum=version.content_checksum,
        created_at=version.created_at,
        step_count=len(version.steps),
        steps=[
            TemplateStepRead(
                position=step.position,
                name=step.name,
                description=step.description,
                script=step.script,
                interpreter=step.interpreter,
                timeout_seconds=step.timeout_seconds,
                rule=_rule_read(step),
                run_as_username=step.run_as_username,
                run_as_method=step.run_as_method,
            )
            for step in version.steps
        ],
    )


# --------------------------------------------------------------------------- #
# Sharing
# --------------------------------------------------------------------------- #

@router.patch(
    "/{template_id}/visibility",
    response_model=TemplateDetail,
    summary="Owner-only: switch between private and shared",
)
def set_visibility(
    template_id: int,
    payload: TemplateVisibilityUpdate,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> TemplateDetail:
    try:
        template = templates.set_visibility(
            template_id,
            payload.visibility,
            principal=principal,
            remote_addr=client_ip(request),
        )
    except templates.TemplateNotFound as exc:
        _audit_denied(principal, request, template_id, "visibility_not_owned")
        raise _not_found() from exc
    except permissions.NotAuthorized as exc:
        _audit_denied(principal, request, template_id, "visibility_not_owner")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail
        ) from exc
    return _detail(template, principal)


@router.get(
    "/{template_id}/shares",
    response_model=list[ShareRead],
    summary="Owner-only: who has access",
)
def list_shares(
    template_id: int,
    principal: Principal = Depends(require_principal),
) -> list[ShareRead]:
    try:
        template = templates.get(template_id, principal)
    except templates.TemplateNotFound as exc:
        raise _not_found() from exc
    if template.owner_id != principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner can view the share list.",
        )
    return [_share_read(row) for row in templates.shares(template_id)]


@router.post(
    "/{template_id}/shares",
    response_model=list[ShareRead],
    status_code=status.HTTP_201_CREATED,
    summary="Owner-only: grant view or edit",
)
def grant_share(
    template_id: int,
    payload: ShareGrant,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> list[ShareRead]:
    try:
        templates.share(
            template_id,
            target_user_id=payload.user_id,
            level=PermissionLevel(payload.level),
            principal=principal,
            remote_addr=client_ip(request),
        )
    except templates.TemplateNotFound as exc:
        _audit_denied(principal, request, template_id, "share_target_or_template_missing")
        raise _not_found() from exc
    except permissions.NotAuthorized as exc:
        _audit_denied(principal, request, template_id, "share_not_owner")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return [_share_read(row) for row in templates.shares(template_id)]


@router.delete(
    "/{template_id}/shares/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Owner-only: revoke a grant",
)
def revoke_share(
    template_id: int,
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Response:
    try:
        removed = templates.unshare(
            template_id,
            target_user_id=user_id,
            principal=principal,
            remote_addr=client_ip(request),
        )
    except templates.TemplateNotFound as exc:
        _audit_denied(principal, request, template_id, "unshare_not_owner")
        raise _not_found() from exc
    except permissions.NotAuthorized as exc:
        _audit_denied(principal, request, template_id, "unshare_not_owner")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail
        ) from exc
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
