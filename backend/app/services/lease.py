"""Single-writer arbitration for the scheduler.

Only one process may own scheduling. This is **not** consensus and it is not
correct on a replicated or multi-writer database: it is a single-row record in
one SQLite file, resolved by SQLite's serialized writes.

Guarantee, stated precisely
---------------------------
* At most one process holds a live lease at any instant.
* A superseded holder cannot insert an execution row: fencing is a **guarded
  write** performed inside the same transaction as the dispatch, so the outcome
  is decided by the database, not by a read the holder could have cached.
* Any number of duplicate dispatch attempts converge on one execution, because
  the ledger occurrence is written first under a UNIQUE constraint.

Explicitly *not* guaranteed: correctness under clock skew, protection against
out-of-band database edits, or prevention of an already-committed execution
continuing to run. A single dispatch can be attempted, abandoned, and retried by
the survivor; that is bounded and absorbed by the ledger, but it is not zero.

Every acquisition increments ``generation``. That counter is what makes "you
were superseded" observable, and it is why renewal and fencing are conditional
updates rather than unconditional writes.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_session_factory

logger = logging.getLogger(__name__)


def _ttl_seconds() -> int:
    """Lease lifetime from settings.

    Read per call rather than captured at import: the scheduler reads it at
    startup, and tests exercise lease expiry by shortening it.
    """
    from app.core.config import get_settings

    return get_settings().scheduler_lease_seconds

_ACQUIRE = text(
    """
    UPDATE scheduler_lease
       SET holder_id = :holder,
           generation = generation + 1,
           acquired_at = :now,
           heartbeat_at = :now,
           expires_at = :expires,
           released_at = NULL,
           hostname = :hostname,
           pid = :pid
     WHERE id = 1
       AND (released_at IS NOT NULL OR expires_at < :now)
    """
)

_INSERT = text(
    """
    INSERT INTO scheduler_lease
        (id, holder_id, generation, acquired_at, heartbeat_at, expires_at,
         released_at, hostname, pid)
    VALUES (1, :holder, 1, :now, :now, :expires, NULL, :hostname, :pid)
    """
)

# Conditional on holder *and* generation: a renewal can never resurrect a lease
# that was taken over, even if it arrives after the takeover.
_RENEW = text(
    """
    UPDATE scheduler_lease
       SET heartbeat_at = :now, expires_at = :expires
     WHERE id = 1
       AND holder_id = :holder
       AND generation = :generation
       AND released_at IS NULL
    """
)

_RELEASE = text(
    """
    UPDATE scheduler_lease
       SET released_at = :now
     WHERE id = 1 AND holder_id = :holder AND generation = :generation
    """
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LeaseState:
    """What the caller may do, and why not when it may not."""

    held: bool
    generation: int | None = None
    holder_id: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None

    @property
    def description(self) -> str:
        if self.held:
            return "ok"
        return self.reason or "unavailable"


class SchedulerLease:
    """The current process's claim on scheduling.

    ``holder_id`` is minted per instance, so two instances in one process (tests)
    are distinguishable and neither can renew the other's lease.
    """

    def __init__(self, holder_id: str | None = None) -> None:
        self.holder_id = holder_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self.generation: int | None = None
        self._expires_at: datetime | None = None
        self._state = LeaseState(held=False, reason="not_acquired")

    # -- introspection ----------------------------------------------------- #

    @property
    def held(self) -> bool:
        return self._state.held

    @property
    def state(self) -> LeaseState:
        return self._state

    @property
    def expires_at(self) -> datetime | None:
        return self._expires_at

    # -- protocol ---------------------------------------------------------- #

    def acquire(self, *, now: datetime | None = None) -> LeaseState:
        """Take the lease, or report who holds it.

        Two statements, both decided by the database: the conditional UPDATE wins
        when an expired or released lease can be taken; otherwise the row is
        either live (report contention) or absent (INSERT, which loses the race
        loudly rather than silently).
        """
        now = now or _now()
        expires = now + timedelta(seconds=_ttl_seconds())
        session: Session = get_session_factory()()
        try:
            result = session.execute(
                _ACQUIRE,
                {
                    "holder": self.holder_id,
                    "now": now,
                    "expires": expires,
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                },
            )
            if result.rowcount == 1:
                row = session.execute(
                    text("SELECT generation FROM scheduler_lease WHERE id = 1")
                ).first()
                session.commit()
                self.generation = int(row[0])
                self._expires_at = expires
                self._state = LeaseState(
                    held=True,
                    generation=self.generation,
                    holder_id=self.holder_id,
                    expires_at=expires,
                )
                logger.info(
                    "Scheduler lease acquired (generation %s, holder %s)",
                    self.generation,
                    self.holder_id,
                )
                return self._state

            existing = session.execute(
                text(
                    "SELECT holder_id, generation, expires_at, released_at "
                    "FROM scheduler_lease WHERE id = 1"
                )
            ).first()

            if existing is None:
                try:
                    session.execute(
                        _INSERT,
                        {
                            "holder": self.holder_id,
                            "now": now,
                            "expires": expires,
                            "hostname": socket.gethostname(),
                            "pid": os.getpid(),
                        },
                    )
                    session.commit()
                except Exception:
                    session.rollback()
                    self._state = LeaseState(held=False, reason="lease_held_by_other")
                    return self._state
                self.generation = 1
                self._expires_at = expires
                self._state = LeaseState(
                    held=True,
                    generation=1,
                    holder_id=self.holder_id,
                    expires_at=expires,
                )
                logger.info("Scheduler lease created (generation 1)")
                return self._state

            session.rollback()
            self._state = LeaseState(
                held=False,
                holder_id=existing[0],
                generation=int(existing[1]),
                reason="lease_held_by_other",
            )
            logger.warning(
                "Scheduler lease is held by %s (generation %s, expires %s); "
                "this process will not schedule",
                existing[0],
                existing[1],
                existing[2],
            )
            return self._state
        finally:
            session.close()

    def renew(self, *, now: datetime | None = None) -> bool:
        """Extend the lease. False means this process has been superseded."""
        if self.generation is None:
            return False
        now = now or _now()
        expires = now + timedelta(seconds=_ttl_seconds())
        session: Session = get_session_factory()()
        try:
            result = session.execute(
                _RENEW,
                {
                    "holder": self.holder_id,
                    "generation": self.generation,
                    "now": now,
                    "expires": expires,
                },
            )
            session.commit()
            if result.rowcount == 1:
                self._expires_at = expires
                return True
            self._state = LeaseState(
                held=False,
                generation=self.generation,
                holder_id=self.holder_id,
                reason="lease_lost",
            )
            logger.error(
                "Scheduler lease lost (holder %s, generation %s). "
                "This process will stop scheduling.",
                self.holder_id,
                self.generation,
            )
            return False
        finally:
            session.close()

    def validate_and_touch(self, session: Session, *, now: datetime | None = None) -> bool:
        """Fence a dispatch by touching the lease **inside the caller's transaction**.

        This is the only correct way to fence. A read of ``generation`` cannot
        detect a takeover that happens immediately afterwards; a guarded write in
        the same transaction as the execution insert is decided by the database,
        so either this update wins or the takeover committed first. On success the
        caller's transaction is left dirty with the renewed lease.
        """
        if self.generation is None:
            return False
        now = now or _now()
        expires = now + timedelta(seconds=_ttl_seconds())
        result = session.execute(
            _RENEW,
            {
                "holder": self.holder_id,
                "generation": self.generation,
                "now": now,
                "expires": expires,
            },
        )
        if result.rowcount == 1:
            self._expires_at = expires
            return True
        self._state = LeaseState(
            held=False,
            generation=self.generation,
            holder_id=self.holder_id,
            reason="lease_lost",
        )
        return False

    def release(self, *, now: datetime | None = None) -> None:
        """Give the lease up on clean shutdown so a restart need not wait the TTL."""
        if self.generation is None:
            return
        now = now or _now()
        session: Session = get_session_factory()()
        try:
            session.execute(
                _RELEASE,
                {
                    "holder": self.holder_id,
                    "generation": self.generation,
                    "now": now,
                },
            )
            session.commit()
        except Exception:  # pragma: no cover - shutdown must not raise
            session.rollback()
            logger.warning("Could not release the scheduler lease", exc_info=True)
        finally:
            session.close()
        self._state = LeaseState(held=False, generation=self.generation, reason="released")


def read_lease() -> LeaseState:
    """Current lease state, for health reporting. Never mutates."""
    from sqlalchemy import select

    from app.models.schedule import SchedulerLease as LeaseModel

    session: Session = get_session_factory()()
    try:
        row = session.scalars(select(LeaseModel).where(LeaseModel.id == 1)).first()
        if row is None:
            return LeaseState(held=False, reason="not_acquired")
        expires = _as_utc(row.expires_at)
        released = row.released_at is not None
        live = not released and expires >= _now()
        return LeaseState(
            held=live,
            holder_id=row.holder_id,
            generation=row.generation,
            expires_at=expires,
            reason=None if live else ("released" if released else "expired"),
        )
    finally:
        session.close()


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; reinterpreting as UTC is correct here."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
