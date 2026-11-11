"""Database-backed edit locks.

One active editor per resource. Durability and atomicity come from the database,
not from a process-local dictionary, so the guarantee holds across restarts and
would hold across multiple workers too.

Concurrency is settled by a **partial unique index** on
``(resource_type, resource_id) WHERE released_at IS NULL``. Two simultaneous
acquirers race on ``INSERT``; exactly one commits and the other gets an
``IntegrityError``, which is reported as a 409 with the current holder. Application
code never "checks then inserts", because that check is not atomic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.db.types import as_utc, utcnow
from app.models.audit_actions import AuditAction
from app.models.permission import ResourceLock, ResourceType
from app.models.user import User
from app.services import audit

logger = logging.getLogger(__name__)

LOCK_TTL_MINUTES = 10


class LockConflict(Exception):
    """Another editor holds the lock."""

    def __init__(self, holder: LockInfo) -> None:
        super().__init__("Resource is locked by another editor.")
        self.holder = holder


class LockRequired(Exception):
    """The caller must hold the lock to perform this action."""


class StaleVersion(Exception):
    """The client's ``row_version`` is older than the stored one."""

    def __init__(self, current_version: int) -> None:
        super().__init__("The resource has changed since it was loaded.")
        self.current_version = current_version


@dataclass(frozen=True)
class LockInfo:
    """Serialisable view of a lock, including who holds it."""

    resource_type: str
    resource_id: int
    user_id: int
    username: str
    display_name: str | None
    session_id: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    is_mine: bool

    @property
    def holder_label(self) -> str:
        return self.display_name or self.username


def _lookup_active(session: Session, resource_type: ResourceType, resource_id: int):
    return session.scalar(
        select(ResourceLock).where(
            ResourceLock.resource_type == resource_type,
            ResourceLock.resource_id == resource_id,
            ResourceLock.released_at.is_(None),
        )
    )


def _describe(session: Session, lock: ResourceLock, *, user_id: int) -> LockInfo:
    user = session.get(User, lock.user_id)
    return LockInfo(
        resource_type=lock.resource_type.value,
        resource_id=lock.resource_id,
        user_id=lock.user_id,
        username=user.username if user else "unknown",
        display_name=user.display_name if user else None,
        session_id=lock.session_id,
        acquired_at=lock.acquired_at,
        renewed_at=lock.renewed_at,
        expires_at=lock.expires_at,
        is_mine=lock.user_id == user_id,
    )


def _expire_if_due(session: Session, lock: ResourceLock) -> bool:
    """Release an expired lock in place. Returns True when it was expired."""
    if as_utc(lock.expires_at) > utcnow():
        return False
    lock.released_at = utcnow()
    lock.released_reason = "expired"
    session.commit()
    audit.record(
        AuditAction.LOCK_EXPIRED,
        actor_id=lock.user_id,
        actor_username=_label_for(session, lock.user_id),
        resource_type=lock.resource_type.value,
        resource_id=lock.resource_id,
        detail={"expired_at": lock.expires_at.isoformat()},
    )
    return True


def _label_for(session: Session, user_id: int) -> str | None:
    user = session.get(User, user_id)
    return user.label if user else None


def acquire(
    resource_type: ResourceType,
    resource_id: int,
    *,
    user_id: int,
    session_id: int,
    ttl_minutes: int = LOCK_TTL_MINUTES,
    remote_addr: str | None = None,
) -> LockInfo:
    """Take the lock, or raise ``LockConflict`` with the current holder.

    Idempotent for the same user *and* session: re-acquiring refreshes the lease
    instead of conflicting with yourself. A second session of the same user is a
    different editor and must not silently share the lock.
    """
    session = get_session_factory()()
    try:
        existing = _lookup_active(session, resource_type, resource_id)
        if existing is not None:
            if _expire_if_due(session, existing):
                existing = None
            elif existing.user_id == user_id and existing.session_id == session_id:
                return _renew_in_place(session, existing, ttl_minutes)
            else:
                raise LockConflict(_describe(session, existing, user_id=user_id))

        now = utcnow()
        lock = ResourceLock(
            resource_type=resource_type,
            resource_id=resource_id,
            user_id=user_id,
            session_id=session_id,
            acquired_at=now,
            renewed_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        session.add(lock)
        try:
            session.commit()
        except IntegrityError:
            # Lost the race. Report whoever actually won.
            session.rollback()
            winner = _lookup_active(session, resource_type, resource_id)
            if winner is None:  # pragma: no cover - would mean a released row
                raise LockConflict(
                    LockInfo(
                        resource_type=resource_type.value,
                        resource_id=resource_id,
                        user_id=0,
                        username="unknown",
                        display_name=None,
                        session_id=0,
                        acquired_at=now,
                        renewed_at=now,
                        expires_at=now,
                        is_mine=False,
                    )
                ) from None
            raise LockConflict(_describe(session, winner, user_id=user_id)) from None

        session.refresh(lock)
        audit.record(
            AuditAction.LOCK_ACQUIRED,
            actor_id=user_id,
            actor_username=_label_for(session, user_id),
            resource_type=resource_type.value,
            resource_id=resource_id,
            session_id=session_id,
            remote_addr=remote_addr,
            detail={"expires_at": lock.expires_at.isoformat()},
        )
        return _describe(session, lock, user_id=user_id)
    finally:
        session.close()


def _renew_in_place(session: Session, lock: ResourceLock, ttl_minutes: int) -> LockInfo:
    lock.renewed_at = utcnow()
    lock.expires_at = utcnow() + timedelta(minutes=ttl_minutes)
    session.commit()
    session.refresh(lock)
    return _describe(session, lock, user_id=lock.user_id)


def renew(
    resource_type: ResourceType,
    resource_id: int,
    *,
    user_id: int,
    session_id: int,
    ttl_minutes: int = LOCK_TTL_MINUTES,
) -> LockInfo:
    """Extend an existing lease. Raises ``LockRequired`` if it is gone."""
    session = get_session_factory()()
    try:
        lock = _lookup_active(session, resource_type, resource_id)
        if lock is None or lock.user_id != user_id or lock.session_id != session_id:
            raise LockRequired("You no longer hold the edit lock for this resource.")
        if _expire_if_due(session, lock):
            raise LockRequired("Your edit lock expired.")

        info = _renew_in_place(session, lock, ttl_minutes)
        audit.record(
            AuditAction.LOCK_RENEWED,
            actor_id=user_id,
            actor_username=info.holder_label,
            resource_type=resource_type.value,
            resource_id=resource_id,
            session_id=session_id,
            detail={"expires_at": info.expires_at.isoformat()},
        )
        return info
    finally:
        session.close()


def release(
    resource_type: ResourceType,
    resource_id: int,
    *,
    user_id: int | None = None,
    session_id: int | None = None,
    force: bool = False,
    reason: str = "released",
) -> bool:
    """Release a lock.

    Owners release their own. ``force=True`` is for logout or account teardown,
    where the session is going away and the lock must not outlive it.
    """
    session = get_session_factory()()
    try:
        lock = _lookup_active(session, resource_type, resource_id)
        if lock is None:
            return False
        if not force and (lock.user_id != user_id or lock.session_id != session_id):
            return False
        lock.released_at = utcnow()
        lock.released_reason = reason
        session.commit()
        audit.record(
            AuditAction.LOCK_RELEASED,
            actor_id=user_id if user_id is not None else lock.user_id,
            actor_username=_label_for(session, lock.user_id),
            resource_type=resource_type.value,
            resource_id=resource_id,
            session_id=session_id or lock.session_id,
            detail={"reason": reason},
        )
        return True
    finally:
        session.close()


def release_all_for_session(session_id: int, *, reason: str = "logout") -> int:
    """Release every lock a session holds. Called on logout and revocation."""
    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(ResourceLock).where(
                    ResourceLock.session_id == session_id,
                    ResourceLock.released_at.is_(None),
                )
            )
        )
        for row in rows:
            row.released_at = utcnow()
            row.released_reason = reason
        session.commit()
        return len(rows)
    finally:
        session.close()


def current_holder(
    resource_type: ResourceType, resource_id: int, *, user_id: int
) -> LockInfo | None:
    """Who holds the lock now, if anyone. Expired locks are reclaimed here."""
    session = get_session_factory()()
    try:
        lock = _lookup_active(session, resource_type, resource_id)
        if lock is None:
            return None
        if _expire_if_due(session, lock):
            return None
        return _describe(session, lock, user_id=user_id)
    finally:
        session.close()


def assert_holds(
    resource_type: ResourceType,
    resource_id: int,
    *,
    user_id: int,
    session_id: int,
) -> None:
    """Guard for a write that requires the lock. Raises ``LockRequired``."""
    session = get_session_factory()()
    try:
        lock = _lookup_active(session, resource_type, resource_id)
        if lock is None:
            raise LockRequired("Acquire the edit lock before modifying this resource.")
        if _expire_if_due(session, lock):
            raise LockRequired("Your edit lock expired; re-acquire it.")
        if lock.user_id != user_id or lock.session_id != session_id:
            raise LockConflict(_describe(session, lock, user_id=user_id))
    finally:
        session.close()


def check_version(expected: int | None, current: int) -> None:
    """Optimistic concurrency check. ``None`` means the client did not supply one."""
    if expected is not None and expected != current:
        raise StaleVersion(current)


def reclaim_expired(*, limit: int = 100) -> int:
    """Housekeeping: release locks past their lease. Returns how many."""
    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(ResourceLock)
                .where(ResourceLock.released_at.is_(None), ResourceLock.expires_at <= utcnow())
                .limit(limit)
            )
        )
        for row in rows:
            row.released_at = utcnow()
            row.released_reason = "expired"
        session.commit()
        for row in rows:
            audit.record(
                AuditAction.LOCK_EXPIRED,
                actor_id=row.user_id,
                actor_username=_label_for(session, row.user_id),
                resource_type=row.resource_type.value,
                resource_id=row.resource_id,
                detail={"reason": "lease elapsed"},
            )
        return len(rows)
    finally:
        session.close()
