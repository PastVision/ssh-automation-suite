"""SSH credential endpoints.

Three routes: list, create, delete. That is the smallest surface that lets a user
author a runnable workflow, and it is deliberately not a full CRUD: editing is
"create a replacement, then delete the old one".

Conventions mirrored from the template and workflow routes so a client sees one
consistent contract:

* A credential the caller does not own answers **404**, never 403, so ids cannot
  be probed.
* A duplicate name answers **409**; an unusable key answers **400**.
* A delete still referenced by a workflow answers **409** with reference counts.

No response model can carry key material: ``CredentialRead`` has no such field.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.models.audit_actions import AuditAction, AuditOutcome
from app.schemas.credential import (
    CredentialCreate,
    CredentialDeleteBlocked,
    CredentialListResponse,
    CredentialRead,
)
from app.services import audit, credentials, sessions
from app.services.credentials import CredentialNotFound
from app.services.identity import client_ip, require_principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credentials", tags=["credentials"])


def _read(credential) -> CredentialRead:
    return CredentialRead(
        id=credential.id,
        name=credential.name,
        username=credential.username,
        description=credential.description,
        default_port=credential.default_port,
        private_key_fingerprint=credential.private_key_fingerprint,
        private_key_type=credential.private_key_type,
        private_key_comment=credential.private_key_comment,
        has_private_key=bool(credential.private_key_path),
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


def _denied(
    principal: sessions.Principal,
    request: Request,
    credential_id: int,
    reason: str,
) -> None:
    audit.record(
        AuditAction.ACCESS_DENIED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        outcome=AuditOutcome.DENIED,
        resource_type="credential",
        resource_id=credential_id,
        remote_addr=client_ip(request),
        detail={"reason": reason},
    )


@router.get(
    "", response_model=CredentialListResponse, summary="List my SSH credentials"
)
def list_credentials(
    principal: sessions.Principal = Depends(require_principal),
) -> CredentialListResponse:
    rows = credentials.list_for_user(principal.user_id)
    return CredentialListResponse(items=[_read(row) for row in rows])


@router.post(
    "",
    response_model=CredentialRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an SSH credential",
)
def create_credential(
    payload: CredentialCreate,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> CredentialRead:
    try:
        credential = credentials.create(
            name=payload.name,
            username=payload.username,
            private_key=payload.private_key,
            passphrase=payload.passphrase,
            description=payload.description,
            default_port=payload.default_port,
            principal=principal,
            remote_addr=client_ip(request),
        )
    except credentials.CredentialConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (credentials.CredentialInvalid, ValueError) as exc:
        # A validation failure is recorded, but the exception text (which could in
        # principle quote input) is never logged and never echoed back verbatim.
        logger.info("Rejected credential create for user %s", principal.user_id)
        audit.record(
            AuditAction.CREDENTIAL_CREATED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            outcome=AuditOutcome.FAILURE,
            resource_type="credential",
            remote_addr=client_ip(request),
            detail={"reason": "invalid_credential"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _read(credential)


@router.delete(
    "/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an SSH credential I own",
    responses={409: {"description": "The credential is still referenced"}},
)
def delete_credential(
    credential_id: int,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> Response:
    try:
        credentials.delete(
            credential_id, principal=principal, remote_addr=client_ip(request)
        )
    except CredentialNotFound as exc:
        _denied(principal, request, credential_id, "credential_not_deletable")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found"
        ) from exc
    except credentials.CredentialConflict as exc:
        usage = credentials.references(credential_id, principal.user_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CredentialDeleteBlocked(
                message=str(exc),
                workflows=usage["workflows"],
                active_executions=usage["active_executions"],
            ).model_dump(),
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
