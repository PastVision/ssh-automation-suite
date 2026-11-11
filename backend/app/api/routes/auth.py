"""Authentication endpoints.

Login issues a server-side session and sets two cookies: the session token
(HttpOnly, SameSite=Lax, Secure) and a CSRF token that JavaScript must read. Only
the session cookie is secret; the CSRF cookie is intentionally readable so the
SPA can echo it in a header.

In ``proxy`` mode there is no login endpoint at all: identity arrives on every
request from the trusted proxy, so ``/auth/login`` answers 409 to make the
misconfiguration obvious instead of issuing a cookie that would never be used.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.core.config import Settings
from app.models.audit_actions import AuditAction, AuditOutcome
from app.schemas.auth import (
    LoginRequest,
    MeResponse,
    SessionRead,
    SessionStatus,
    UserRead,
)
from app.services import audit, locks
from app.services.identity import (
    client_ip,
    get_request_settings,
    require_principal,
    resolve_principal,
)
from app.services.sessions import (
    AuthenticationError,
    Principal,
    authenticate_local,
    create_session,
    get_user,
    list_sessions,
    revoke_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _session_read(row) -> SessionRead:
    return SessionRead(
        id=row.id,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoked_reason=row.revoked_reason,
        auth_source=row.auth_source,
        user_agent=row.user_agent,
        remote_addr=row.remote_addr,
    )




def _user_read(user, principal: Principal | None = None) -> UserRead:
    if user is None:
        return UserRead(
            id=principal.user_id,
            username=principal.username,
            display_name=principal.display_name,
            is_active=True,
        )
    return UserRead(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


def _session_row(principal: Principal):
    """The caller's own session row, when this request used a cookie session."""
    if principal.session_id is None:
        return None
    return next(
        (row for row in list_sessions(principal.user_id) if row.id == principal.session_id),
        None,
    )
def _set_session_cookies(
    response: Response, settings: Settings, token: str, csrf: str, absolute
) -> None:
    max_age = max(0, int(absolute.timestamp() - time.time()))
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,
        secure=settings.resolved_cookie_secure,
        samesite="lax",
        path="/",
        max_age=max_age,
    )
    # Readable by the SPA on purpose: this is the double-submit half of CSRF.
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf,
        httponly=False,
        secure=settings.resolved_cookie_secure,
        samesite="lax",
        path="/",
        max_age=max_age,
    )


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(key=name, path="/", samesite="lax")


@router.post("/login", response_model=MeResponse, summary="Sign in")
def login(
    payload: LoginRequest, request: Request, response: Response
) -> MeResponse:
    settings = get_request_settings(request)
    if settings.auth_mode == "proxy":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This deployment authenticates through a trusted proxy; "
                "password sign-in is not enabled."
            ),
        )

    try:
        user = authenticate_local(
            payload.username, payload.password, remote_addr=client_ip(request)
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    issued = create_session(
        user.id,
        settings=settings,
        user_agent=request.headers.get("user-agent"),
        remote_addr=client_ip(request),
        source="local",
    )
    _set_session_cookies(
        response,
        settings,
        issued.token,
        issued.csrf_token,
        issued.absolute_expires_at,
    )

    audit.record(
        AuditAction.LOGIN_SUCCEEDED,
        actor_id=user.id,
        actor_username=user.label,
        session_id=issued.session_id,
        remote_addr=client_ip(request),
        detail={"auth_source": "local"},
    )

    rows = {row.id: row for row in list_sessions(user.id)}
    return MeResponse(
        user=_user_read(user),
        auth_mode=settings.auth_mode,
        csrf_token=issued.csrf_token,
        session=_session_read(rows[issued.session_id]) if issued.session_id in rows else None,
    )


@router.get("/me", response_model=MeResponse, summary="Current user")
def me(request: Request, principal: Principal = Depends(require_principal)) -> MeResponse:
    settings = get_request_settings(request)

    user = get_user(principal.user_id)
    row = _session_row(principal)

    return MeResponse(
        user=_user_read(user, principal),
        auth_mode=settings.auth_mode,
        csrf_token=principal.csrf_token if settings.auth_mode == "local" else None,
        session=_session_read(row) if row else None,
    )


@router.get("/status", response_model=SessionStatus, summary="Session status")
def status_endpoint(request: Request) -> SessionStatus:
    """Unauthenticated-friendly probe so the SPA knows whether to show the login page."""
    settings = get_request_settings(request)
    from app.services.sessions import get_user

    try:
        principal = resolve_principal(request)
    except HTTPException:
        return SessionStatus(authenticated=False, auth_mode=settings.auth_mode)

    user = get_user(principal.user_id)
    row = _session_row(principal)

    return SessionStatus(
        authenticated=True,
        auth_mode=settings.auth_mode,
        user=_user_read(user, principal),
        csrf_token=principal.csrf_token if settings.auth_mode == "local" else None,
        session=_session_read(row) if row else None,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
def logout(
    request: Request,
    response: Response,
    principal: Principal = Depends(require_principal),
) -> Response:
    settings = get_request_settings(request)

    if principal.session_id is not None:
        # Release this session's edit locks first: a lock held by a dead session
        # would otherwise block other editors until its lease elapsed.
        released = locks.release_all_for_session(principal.session_id, reason="logout")
        revoke_session(principal.session_id, reason="logout")
        audit.record(
            AuditAction.LOGOUT,
            actor_id=principal.user_id,
            actor_username=principal.label,
            session_id=principal.session_id,
            remote_addr=client_ip(request),
            detail={"locks_released": released},
        )
    else:
        audit.record(
            AuditAction.LOGOUT,
            actor_id=principal.user_id,
            actor_username=principal.label,
            remote_addr=client_ip(request),
            detail={"auth_source": principal.source},
        )

    _clear_session_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/sessions", response_model=list[SessionRead], summary="My sessions")
def my_sessions(principal: Principal = Depends(require_principal)) -> list[SessionRead]:
    return [_session_read(row) for row in list_sessions(principal.user_id)]


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke one of my sessions",
)
def revoke_one(
    session_id: int,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Response:
    """Revoke a session **owned by the caller**.

    The ownership check is the security boundary: without it, any signed-in user
    could log out anybody else by guessing a small integer.
    """
    mine = [row for row in list_sessions(principal.user_id) if row.id == session_id]
    if not mine:
        audit.record(
            AuditAction.ACCESS_DENIED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            outcome=AuditOutcome.DENIED,
            resource_type="session",
            resource_id=session_id,
            remote_addr=client_ip(request),
            detail={"reason": "not_session_owner"},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    locks.release_all_for_session(session_id, reason="session_revoked")
    revoke_session(session_id, reason="revoked_by_user")
    audit.record(
        AuditAction.SESSION_REVOKED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        resource_type="session",
        resource_id=session_id,
        remote_addr=client_ip(request),
        detail={"reason": "revoked_by_user"},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
