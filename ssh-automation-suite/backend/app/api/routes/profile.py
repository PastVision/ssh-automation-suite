"""The caller's own SSH profile.

Every route here operates on ``principal.user_id`` and takes no user identifier
from the request. That is the whole isolation mechanism: there is no route shape
that could address another user's profile, so there is nothing to forget to
check.

Secret endpoints are separate from the settings endpoint so a routine settings
save can never accidentally overwrite or clear a credential, and so each secret
change gets its own audit event.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.models.audit_actions import AuditAction, AuditOutcome
from app.schemas.profile import (
    PassphraseUpdate,
    PrivateKeyUpload,
    ProfileRead,
    ProfileUpdate,
    SecretStateRead,
    SudoPasswordUpdate,
)
from app.services import audit, profiles, sessions
from app.services.identity import client_ip, require_principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profile", tags=["profile"])


def _read(view: profiles.ProfileView) -> ProfileRead:
    return ProfileRead(
        user_id=view.user_id,
        ssh_username=view.ssh_username,
        default_port=view.default_port,
        connect_timeout_seconds=view.connect_timeout_seconds,
        command_timeout_seconds=view.command_timeout_seconds,
        keepalive_seconds=view.keepalive_seconds,
        secrets=SecretStateRead(
            private_key_configured=view.secrets.private_key_configured,
            private_key_fingerprint=view.secrets.private_key_fingerprint,
            private_key_type=view.secrets.private_key_type,
            private_key_comment=view.secrets.private_key_comment,
            passphrase_configured=view.secrets.passphrase_configured,
            sudo_password_configured=view.secrets.sudo_password_configured,
            sudo_password_updated_at=view.secrets.sudo_password_updated_at,
        ),
    )


@router.get("", response_model=ProfileRead, summary="My SSH profile")
def read_profile(
    principal: sessions.Principal = Depends(require_principal),
) -> ProfileRead:
    return _read(profiles.view(principal.user_id))


@router.patch("", response_model=ProfileRead, summary="Update my SSH settings")
def update_profile(
    payload: ProfileUpdate,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> ProfileRead:
    try:
        view = profiles.update_settings(
            principal.user_id,
            ssh_username=payload.ssh_username,
            default_port=payload.default_port,
            connect_timeout_seconds=payload.connect_timeout_seconds,
            command_timeout_seconds=payload.command_timeout_seconds,
            keepalive_seconds=payload.keepalive_seconds,
            clear_timeouts=payload.clear_timeouts,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    audit.record(
        AuditAction.PROFILE_UPDATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="profile",
        resource_id=principal.user_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={
            "fields": sorted(
                name
                for name, value in payload.model_dump(exclude_unset=True).items()
                if value is not None
            )
        },
    )
    return _read(view)


# --------------------------------------------------------------------------- #
# Private key
# --------------------------------------------------------------------------- #

@router.put(
    "/private-key",
    response_model=SecretStateRead,
    summary="Set or replace my private key",
)
def set_private_key(
    payload: PrivateKeyUpload,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> SecretStateRead:
    try:
        state = profiles.set_private_key(
            principal.user_id, payload.private_key, payload.passphrase
        )
    except Exception as exc:
        # The key text is never included in the error or the log.
        logger.info("Rejected private key upload for user %s", principal.user_id)
        audit.record(
            AuditAction.SECRET_SET,
            actor_id=principal.user_id,
            actor_username=principal.label,
            outcome=AuditOutcome.FAILURE,
            resource_type="profile",
            resource_id=principal.user_id,
            session_id=principal.session_id,
            remote_addr=client_ip(request),
            detail={"changed": "private_key", "reason": "invalid_key"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That private key could not be read. Check the format and passphrase.",
        ) from exc

    audit.record(
        AuditAction.SECRET_SET,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="profile",
        resource_id=principal.user_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={
            "changed": "private_key",
            "key_type": state.private_key_type,
            "fingerprint": state.private_key_fingerprint,
            "passphrase_set": state.passphrase_configured,
        },
    )
    return _secret_state(state)


@router.delete(
    "/private-key", response_model=SecretStateRead, summary="Remove my private key"
)
def delete_private_key(
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> SecretStateRead:
    state = profiles.clear_private_key(principal.user_id)
    audit.record(
        AuditAction.SECRET_CLEARED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="profile",
        resource_id=principal.user_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={"changed": "private_key"},
    )
    return _secret_state(state)


# --------------------------------------------------------------------------- #
# Key passphrase
# --------------------------------------------------------------------------- #

@router.put(
    "/key-passphrase",
    response_model=SecretStateRead,
    summary="Set my key passphrase",
)
def set_passphrase(
    payload: PassphraseUpdate,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> SecretStateRead:
    state = profiles.set_passphrase(principal.user_id, payload.passphrase)
    audit.record(
        AuditAction.SECRET_SET,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="profile",
        resource_id=principal.user_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={"changed": "key_passphrase"},
    )
    return _secret_state(state)


@router.delete(
    "/key-passphrase",
    response_model=SecretStateRead,
    summary="Clear my key passphrase",
)
def delete_passphrase(
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> SecretStateRead:
    state = profiles.clear_passphrase(principal.user_id)
    audit.record(
        AuditAction.SECRET_CLEARED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="profile",
        resource_id=principal.user_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={"changed": "key_passphrase"},
    )
    return _secret_state(state)


# --------------------------------------------------------------------------- #
# Primary sudo password
# --------------------------------------------------------------------------- #

@router.put(
    "/sudo-password",
    response_model=SecretStateRead,
    summary="Set my primary sudo password",
)
def set_sudo_password(
    payload: SudoPasswordUpdate,
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> SecretStateRead:
    state = profiles.set_sudo_password(principal.user_id, payload.password)
    audit.record(
        AuditAction.SECRET_SET,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="profile",
        resource_id=principal.user_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={"changed": "primary_sudo_password"},
    )
    return _secret_state(state)


@router.delete(
    "/sudo-password",
    response_model=SecretStateRead,
    summary="Clear my primary sudo password",
)
def delete_sudo_password(
    request: Request,
    principal: sessions.Principal = Depends(require_principal),
) -> SecretStateRead:
    state = profiles.clear_sudo_password(principal.user_id)
    audit.record(
        AuditAction.SECRET_CLEARED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="profile",
        resource_id=principal.user_id,
        session_id=principal.session_id,
        remote_addr=client_ip(request),
        detail={"changed": "primary_sudo_password"},
    )
    return _secret_state(state)


def _secret_state(state: profiles.SecretState) -> SecretStateRead:
    return SecretStateRead(
        private_key_configured=state.private_key_configured,
        private_key_fingerprint=state.private_key_fingerprint,
        private_key_type=state.private_key_type,
        private_key_comment=state.private_key_comment,
        passphrase_configured=state.passphrase_configured,
        sudo_password_configured=state.sudo_password_configured,
        sudo_password_updated_at=state.sudo_password_updated_at,
    )
