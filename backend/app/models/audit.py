"""Append-only audit history.

``audit_events`` is written only by ``app.services.audit``; no route exposes an
update or delete. The database enforces this too (see the Alembic revision, which
installs ``BEFORE UPDATE``/``BEFORE DELETE`` triggers that abort), so a future
buggy handler still cannot rewrite history.

``actor_username`` is a *snapshot* of the display name at the time of the event.
It is intentionally denormalised: renaming or deleting a user must not change
what an old record says happened. ``actor_user_id`` is null only for events with
no authenticated actor (a failed login for an unknown name).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import timestamp_column


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_created_at", "created_at"),
        Index("ix_audit_events_action_created_at", "action", "created_at"),
        Index("ix_audit_events_resource", "resource_type", "resource_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    actor_user_id: Mapped[int | None] = mapped_column(
        # RESTRICT, not SET NULL: the audit table is append-only, and SET NULL is
        # an UPDATE the append-only trigger rejects. Accounts are deactivated, so
        # history keeps its attribution and deletion is refused explicitly.
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    actor_username: Mapped[str | None] = mapped_column(String(120), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success")

    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Non-secret context only. ``app.services.audit`` rejects known secret keys,
    # but callers must still never pass secret values.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Where the request came from. Helpful for investigating an incident; never
    # used for authorization.
    remote_addr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = timestamp_column()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditEvent id={self.id} action={self.action!r} outcome={self.outcome!r}>"
