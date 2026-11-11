"""Session lifecycle and identity resolution.

Two identity sources, one ``Principal`` type for the rest of the application:

* ``local`` -- the browser posts a username and password; this module verifies the
  scrypt hash and issues a session.
* ``proxy`` -- a trusted reverse proxy has already authenticated the user and
  asserts the identity on every request, so there is no password and no session
  cookie. Trust is limited to configured peer networks (and an optional shared
  secret), and is never inferred from the header alone.

Sessions are server-side rows. The browser holds only an opaque token whose
SHA-256 digest is stored, so a database read cannot impersonate anybody.

Expiry is enforced here, not by the cookie: ``expires_at`` is refreshed on use
(idle timeout) and never beyond ``absolute_expires_at`` (absolute cap).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.passwords import (
    generate_csrf_token,
    generate_session_token,
    hash_password,
    hash_session_token,
    needs_rehash,
    verify_password,
)
from app.db.session import get_session_factory
from app.db.types import as_utc, utcnow
from app.models.audit_actions import AuditAction, AuditOutcome
from app.models.user import User, UserSession
from app.services import audit

logger = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Credentials or asserted identity were not accepted."""


class SessionExpired(AuthenticationError):
    """The session token was valid but its idle or absolute lifetime elapsed."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller for one request."""

    user_id: int
    username: str
    display_name: str | None
    source: str
    session_id: int | None = None
    csrf_token: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.username


@dataclass(frozen=True)
class IssuedSession:
    """A freshly created session. ``token`` is the only time it exists in plain."""

    token: str
    csrf_token: str
    session_id: int
    expires_at: datetime
    absolute_expires_at: datetime


def _now() -> datetime:
    return utcnow()


def _build_dummy_password_hash() -> str:
    """A real scrypt hash of random text, used to equalise the unknown-user path.

    The verifier must do the same KDF work whether or not the account exists, or
    the response time discloses which usernames are real. The placeholder has to be
    *precomputed*: hashing inside the unknown-user branch made that branch do two
    derivations against one for a real account (a measured ~1.8x gap), and a lazy
    cache only moved the gap to the first probe of each process. Built from random
    bytes so it can never match a submitted password.
    """
    return hash_password(secrets.token_urlsafe(32))


# Computed once at import, not on demand: a lazy value would leave the very first
# unknown-username login measurably slower than a known one, which is enough to
# enumerate accounts. One scrypt at startup is a fair price for constant work.
DUMMY_PASSWORD_HASH: str = _build_dummy_password_hash()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def get_user(user_id: int) -> User | None:
    session = get_session_factory()()
    try:
        return session.get(User, user_id)
    finally:
        session.close()


def get_user_by_username(username: str) -> User | None:
    session = get_session_factory()()
    try:
        return session.scalar(select(User).where(User.username == username))
    finally:
        session.close()


def create_user(
    username: str,
    password: str | None = None,
    *,
    display_name: str | None = None,
    external_id: str | None = None,
    is_active: bool = True,
) -> User:
    """Create an account. Used by the CLI, tests, and proxy first-login."""
    username = username.strip()
    if not username:
        raise ValueError("username must not be empty")

    session = get_session_factory()()
    try:
        user = User(
            username=username,
            display_name=display_name or None,
            external_id=external_id,
            password_hash=hash_password(password) if password else None,
            is_active=is_active,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    finally:
        session.close()


def set_password(user_id: int, password: str) -> None:
    session = get_session_factory()()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise ValueError(f"user {user_id} does not exist")
        user.password_hash = hash_password(password)
        user.password_changed_at = _now()
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Local login
# --------------------------------------------------------------------------- #

def authenticate_local(
    username: str, password: str, *, remote_addr: str | None = None
) -> User:
    """Verify a password and return the user, or raise ``AuthenticationError``.

    The same error and roughly the same work happen for "no such user" and "wrong
    password" so the endpoint does not disclose which usernames exist.
    """
    session = get_session_factory()()
    try:
        user = session.scalar(select(User).where(User.username == username))
        stored = user.password_hash if user is not None else None

        # Always run the KDF, even when the account is missing, so response time
        # does not reveal whether the username exists. The placeholder is a
        # precomputed hash: hashing inside this branch would double the work here
        # and reintroduce the oracle (see DUMMY_PASSWORD_HASH).
        if stored is None:
            verify_password(password, DUMMY_PASSWORD_HASH)
            ok = False
        else:
            ok = verify_password(password, stored)

        if not ok or user is None:
            audit.record(
                AuditAction.LOGIN_FAILED,
                actor_id=user.id if user else None,
                actor_username=user.username if user else username,
                outcome=AuditOutcome.FAILURE,
                detail={"reason": "invalid_credentials"},
                remote_addr=remote_addr,
            )
            raise AuthenticationError("Invalid username or password.")

        if not user.is_active:
            audit.record(
                AuditAction.LOGIN_FAILED,
                actor_id=user.id,
                actor_username=user.username,
                outcome=AuditOutcome.DENIED,
                detail={"reason": "inactive_account"},
                remote_addr=remote_addr,
            )
            raise AuthenticationError("This account is disabled.")

        # Opportunistically upgrade a hash created with weaker parameters.
        if needs_rehash(stored):
            user.password_hash = hash_password(password)
            user.password_changed_at = _now()

        user.last_login_at = _now()
        session.commit()
        session.refresh(user)
        return user
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Session creation and validation
# --------------------------------------------------------------------------- #

def create_session(
    user_id: int,
    *,
    settings: Settings | None = None,
    user_agent: str | None = None,
    remote_addr: str | None = None,
    source: str = "local",
) -> IssuedSession:
    settings = settings or get_settings()
    now = _now()
    expires_at = now + timedelta(minutes=settings.session_idle_timeout_minutes)
    absolute = now + timedelta(hours=settings.session_absolute_timeout_hours)

    token = generate_session_token()
    csrf = generate_csrf_token()

    session = get_session_factory()()
    try:
        row = UserSession(
            user_id=user_id,
            token_hash=hash_session_token(token),
            csrf_token=csrf,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            user_agent=(user_agent or "")[:255] or None,
            remote_addr=remote_addr,
            auth_source=source,
        )
        session.add(row)
        session.commit()
        session.refresh(row)

        audit.record(
            AuditAction.SESSION_CREATED,
            actor_id=user_id,
            actor_username=_username_for(session, user_id),
            session_id=row.id,
            remote_addr=remote_addr,
            detail={"auth_source": source, "absolute_expires_at": absolute.isoformat()},
        )
        return IssuedSession(
            token=token,
            csrf_token=csrf,
            session_id=row.id,
            expires_at=expires_at,
            absolute_expires_at=absolute,
        )
    finally:
        session.close()


def _username_for(session, user_id: int) -> str | None:
    user = session.get(User, user_id)
    return user.label if user else None


def resolve_session_token(
    token: str, *, settings: Settings | None = None, touch: bool = True
) -> Principal:
    """Validate a session token and return the caller.

    Rejects revoked, idle-expired, and absolutely-expired sessions. On success the
    idle deadline slides forward, but never past the absolute cap: that cap is
    derived from ``created_at`` so a leaked token cannot be kept alive forever.

    ``touch=False`` skips the write, used by read-only checks such as the CSRF
    comparison so a rejected request cannot extend a session's life.
    """
    settings = settings or get_settings()
    digest = hash_session_token(token)

    session = get_session_factory()()
    try:
        row = session.scalar(select(UserSession).where(UserSession.token_hash == digest))
        if row is None:
            raise AuthenticationError("Invalid session.")

        now = _now()
        if row.revoked_at is not None:
            raise AuthenticationError("Session has been revoked.")
        # as_utc: SQLite returns naive datetimes on read; comparing one to an aware
        # value raises TypeError. See app.db.types.as_utc.
        if as_utc(row.expires_at) <= now:
            _revoke_row(session, row, "idle_timeout")
            raise SessionExpired("Session expired from inactivity.")

        absolute_deadline = as_utc(row.created_at) + timedelta(
            hours=settings.session_absolute_timeout_hours
        )
        if now >= absolute_deadline:
            _revoke_row(session, row, "absolute_timeout")
            raise SessionExpired("Session expired.")

        user = session.get(User, row.user_id)
        if user is None or not user.is_active:
            _revoke_row(session, row, "user_inactive")
            raise AuthenticationError("Session is no longer valid.")

        if touch:
            row.last_seen_at = now
            row.expires_at = min(
                now + timedelta(minutes=settings.session_idle_timeout_minutes),
                absolute_deadline,
            )
            session.commit()

        return Principal(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            source=row.auth_source,
            session_id=row.id,
            csrf_token=row.csrf_token,
        )
    finally:
        session.close()


def _revoke_row(session, row: UserSession, reason: str) -> None:
    row.revoked_at = _now()
    row.revoked_reason = reason
    session.commit()
    audit.record(
        AuditAction.SESSION_REVOKED,
        actor_id=row.user_id,
        actor_username=_username_for(session, row.user_id),
        session_id=row.id,
        detail={"reason": reason},
    )


def revoke_session(session_id: int, *, reason: str = "logout") -> bool:
    session = get_session_factory()()
    try:
        row = session.get(UserSession, session_id)
        if row is None or row.revoked_at is not None:
            return False
        _revoke_row(session, row, reason)
        return True
    finally:
        session.close()


def revoke_all_for_user(user_id: int, *, reason: str = "revoked") -> int:
    """Revoke every live session for a user (password change, admin action)."""
    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(UserSession).where(
                    UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
                )
            )
        )
        for row in rows:
            row.revoked_at = _now()
            row.revoked_reason = reason
        session.commit()
        if rows:
            audit.record(
                AuditAction.SESSION_REVOKED,
                actor_id=user_id,
                actor_username=_username_for(session, user_id),
                detail={"reason": reason, "count": len(rows)},
            )
        return len(rows)
    finally:
        session.close()


def purge_expired_sessions(*, settings: Settings | None = None) -> int:
    """Delete long-expired rows. Kept separate from revocation for auditing."""
    settings = settings or get_settings()
    cutoff = _now() - timedelta(hours=settings.session_absolute_timeout_hours)
    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(select(UserSession).where(UserSession.created_at < cutoff))
        )
        for row in rows:
            session.delete(row)
        session.commit()
        return len(rows)
    finally:
        session.close()


def list_sessions(user_id: int) -> list[UserSession]:
    session = get_session_factory()()
    try:
        return list(
            session.scalars(
                select(UserSession)
                .where(UserSession.user_id == user_id)
                .order_by(UserSession.id.desc())
            )
        )
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Proxy mode
# --------------------------------------------------------------------------- #

def verify_proxy_secret(presented: str | None, settings: Settings) -> bool:
    """Constant-time comparison of the proxy's shared secret."""
    if not settings.trust_proxy_secret:
        # No secret configured (development). The network allowlist is then the
        # only boundary; production refuses this configuration at startup.
        return True
    if not presented:
        return False
    return hmac.compare_digest(presented, settings.trust_proxy_secret)


def resolve_proxy_identity(
    asserted_name: str,
    *,
    asserted_id: str | None = None,
    settings: Settings | None = None,
    remote_addr: str | None = None,
) -> Principal:
    """Map a proxy-asserted identity onto a local user row.

    Users are created on first sight so an operator does not have to pre-register
    everybody. ``external_id`` is the stable key; the username may be renamed
    later without orphaning history.
    """
    settings = settings or get_settings()
    name = (asserted_name or "").strip()
    if not name:
        raise AuthenticationError("The proxy did not assert an identity.")

    external_id = (asserted_id or "").strip() or name

    session = get_session_factory()()
    try:
        user = session.scalar(select(User).where(User.external_id == external_id))
        if user is None:
            # Fall back to a matching username so a local account can be adopted
            # by proxy authentication without a migration.
            user = session.scalar(select(User).where(User.username == name))
            if user is not None and user.external_id is None:
                user.external_id = external_id
                session.commit()
        if user is None:
            user = User(
                username=name,
                display_name=None,
                external_id=external_id,
                password_hash=None,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            audit.record(
                AuditAction.LOGIN_SUCCEEDED,
                actor_id=user.id,
                actor_username=user.label,
                detail={"auth_source": "proxy", "provisioned": True},
                remote_addr=remote_addr,
            )
        if not user.is_active:
            raise AuthenticationError("This account is disabled.")

        return Principal(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            source="proxy",
        )
    finally:
        session.close()


def authenticated_username_header_digest(value: str) -> str:
    """Small helper used by tests to assert a forged header is not trusted."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
