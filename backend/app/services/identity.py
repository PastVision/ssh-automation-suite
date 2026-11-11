"""Request identity resolution and authorization dependencies.

This module is the single place that answers "who is calling?". Nothing else may
build a ``Principal`` from raw request data, and no endpoint may take a user id
from the request body or query string.

Trust rules, in order:

1. ``proxy`` mode: the immediate TCP peer must be inside ``trusted_proxy_cidrs``
   *and* present the configured shared secret. Only then is the identity header
   read. A client that reaches the app directly cannot satisfy the peer check, so
   a forged ``X-Authenticated-User`` is ignored -- and refused, not silently
   treated as anonymous, so the failure is visible in logs and audit.
2. ``local`` mode: the identity comes from a server-side session cookie. The
   header is not consulted at all, in either direction.

The dependency deliberately distinguishes 401 (no valid identity) from 403
(identity is valid but lacks permission) so the UI can react correctly.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from app.core.config import Settings
from app.models.audit_actions import AuditAction, AuditOutcome
from app.services import audit
from app.services.sessions import (
    AuthenticationError,
    Principal,
    SessionExpired,
    resolve_proxy_identity,
    resolve_session_token,
    verify_proxy_secret,
)

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_request_settings(request: Request) -> Settings:
    """The settings the app was created with (tests may override them)."""
    return request.app.state.settings


def client_ip(request: Request) -> str | None:
    """The immediate peer address.

    ``X-Forwarded-For`` is intentionally **not** used: it is client-controlled and
    would let anyone claim to come from a trusted proxy network.
    """
    return request.client.host if request.client else None


def _peer_is_trusted_proxy(request: Request, settings: Settings) -> bool:
    peer = client_ip(request)
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for network in settings.trusted_proxy_cidrs:
        try:
            if address in ipaddress.ip_network(network, strict=False):
                return True
        except ValueError:  # pragma: no cover - validated at startup
            continue
    return False


def _unauthorized(detail: str = "Authentication required.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Cookie"},
    )


def resolve_principal(request: Request) -> Principal:
    """Resolve the caller or raise 401. Never returns an unauthenticated value."""
    settings = get_request_settings(request)

    if settings.auth_mode == "proxy":
        return _resolve_proxy_principal(request, settings)
    return _resolve_cookie_principal(request, settings)


def _resolve_proxy_principal(request: Request, settings: Settings) -> Principal:
    if not _peer_is_trusted_proxy(request, settings):
        # A direct client. Any identity header it sent is ignored; refuse rather
        # than fall through to an anonymous mode.
        asserted = request.headers.get(settings.identity_header)
        if asserted:
            audit.record(
                AuditAction.ACCESS_DENIED,
                actor_id=None,
                actor_username=None,
                outcome=AuditOutcome.DENIED,
                detail={
                    "reason": "identity_header_from_untrusted_peer",
                    "peer": client_ip(request),
                },
                remote_addr=client_ip(request),
            )
            logger.warning(
                "Ignored %s from untrusted peer %s", settings.identity_header, client_ip(request)
            )
        raise _unauthorized("Authentication is provided by the configured proxy.")

    if not verify_proxy_secret(request.headers.get(settings.proxy_shared_secret_header), settings):
        raise _unauthorized("Invalid proxy credentials.")

    asserted = request.headers.get(settings.identity_header)
    if not asserted:
        raise _unauthorized("The proxy did not assert a user identity.")

    asserted_id = (
        request.headers.get(settings.identity_id_header) if settings.identity_id_header else None
    )
    try:
        return resolve_proxy_identity(
            asserted,
            asserted_id=asserted_id,
            settings=settings,
            remote_addr=client_ip(request),
        )
    except AuthenticationError as exc:
        raise _unauthorized(str(exc)) from exc


def _resolve_cookie_principal(request: Request, settings: Settings) -> Principal:
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise _unauthorized()
    try:
        return resolve_session_token(token, settings=settings)
    except SessionExpired as exc:
        raise _unauthorized(str(exc)) from exc
    except AuthenticationError as exc:
        raise _unauthorized(str(exc)) from exc


def require_principal(
    request: Request,
) -> Principal:
    """FastAPI dependency: authenticated caller, or 401."""
    return resolve_principal(request)


def _require_same_origin(request: Request, *, settings: Settings) -> None:
    """Reject a cross-origin sign-in attempt.

    Login is exempt from the CSRF token because no session exists yet, so it needs
    its own guard against login CSRF. Browsers send ``Origin`` on every POST, and a
    hostile page cannot forge it. A request with no ``Origin`` at all is accepted:
    non-browser clients such as the test suite and CLI helpers must still sign in,
    and they are not the attack this protects against.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return

    # In production the UI is served from the same origin as the API behind a
    # reverse proxy, so the deployed origin is not necessarily in cors_origins.
    # Comparing against this request's own Host covers that case.
    host = request.headers.get("host")
    if host and urlsplit(origin).netloc == host:
        return

    allowed = {o.rstrip("/") for o in settings.cors_origins}
    if origin.rstrip("/") in allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Cross-origin sign-in is not allowed.",
    )


def csrf_protector(request: Request) -> None:
    """Reject a state-changing request that lacks a valid CSRF token.

    Applies to cookie-authenticated sessions only. The token must arrive in the
    ``X-CSRF-Token`` **header** and match the value stored server-side. The readable
    CSRF cookie is never accepted as a substitute: doing so would collapse
    double-submit back to single-submit, because a browser attaches that cookie to
    any same-site request that a hostile page could trigger. Requiring the header
    means a cross-origin form post cannot satisfy the check, since reading the
    cookie to copy it into a header requires same-origin access.

    Sign-in is the one exception: it *creates* a session, so it cannot present a
    token for one. It is instead gated on ``Origin``, which stops a hostile page
    from silently logging the victim into an account the attacker controls.

    ``proxy`` mode is exempt because it has no cookie to forge with.
    """
    settings = get_request_settings(request)
    if request.method in SAFE_METHODS:
        return
    if settings.auth_mode == "proxy":
        return

    if request.url.path == f"{settings.api_prefix}/auth/login":
        _require_same_origin(request, settings=settings)
        return

    token = request.cookies.get(settings.cookie_name)
    if not token:
        # Unauthenticated: the endpoint answers 401 on its own.
        return

    try:
        principal = resolve_session_token(token, settings=settings, touch=False)
    except AuthenticationError:
        # Invalid or stale session cookie. It grants no authority, so there is
        # nothing to forge with: let the endpoint answer 401, or let /auth/login
        # replace the dead cookie. Checking for the header before this point
        # would trap a browser holding an expired cookie in a 403 on the very
        # form that fixes it.
        return

    header_value = request.headers.get(settings.csrf_header_name)
    if not header_value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing CSRF token.",
        )

    expected = principal.csrf_token or ""
    if not hmac.compare_digest(header_value, expected):
        audit.record(
            AuditAction.CSRF_REJECTED,
            actor_id=principal.user_id,
            actor_username=principal.label,
            outcome=AuditOutcome.DENIED,
            session_id=principal.session_id,
            remote_addr=client_ip(request),
            detail={"path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token.",
        )

