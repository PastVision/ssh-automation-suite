"""Edit locks and sharing permissions.

Locks are authoritative in the database, never in memory, so they survive a
restart and are visible to every worker. Atomicity comes from a **partial unique
index** that allows only one unreleased lock per resource; a second concurrent
acquirer hits an ``IntegrityError`` instead of silently stealing the lock.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import enum_column, timestamp_column


class ResourceType(StrEnum):
    TEMPLATE = "template"
    WORKFLOW = "workflow"


class PermissionLevel(StrEnum):
    VIEW = "view"
    EDIT = "edit"


class ResourceLock(Base):
    """One active editor per ``(resource_type, resource_id)``.

    ``released_at`` is set on explicit release, on owner logout, and when an
    expired lock is reclaimed, so the same row never blocks a new editor. The
    partial index is what makes simultaneous acquisition safe.
    """

    __tablename__ = "resource_locks"
    __table_args__ = (
        Index(
            "uq_resource_locks_active",
            "resource_type",
            "resource_id",
            unique=True,
            sqlite_where=text("released_at IS NULL"),
        ),
        Index("ix_resource_locks_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_type: Mapped[ResourceType] = mapped_column(
        enum_column(ResourceType, "resource_type"), nullable=False
    )
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # Tied to both the user and the exact session that took the lock, so a
    # second browser session of the same user is still a second editor.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False
    )

    acquired_at: Mapped[datetime] = timestamp_column()
    renewed_at: Mapped[datetime] = timestamp_column()
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(nullable=True)
    released_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ResourceLock {self.resource_type}::{self.resource_id} user={self.user_id}>"
        )


class ResourcePermission(Base):
    """Explicit sharing grant for a shared resource."""

    __tablename__ = "resource_permissions"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "user_id",
            name="uq_resource_permissions_resource_type_resource_id_user_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_type: Mapped[ResourceType] = mapped_column(
        enum_column(ResourceType, "resource_type"), nullable=False
    )
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[PermissionLevel] = mapped_column(
        enum_column(PermissionLevel, "permission_level"),
        nullable=False,
        default=PermissionLevel.VIEW,
    )
    granted_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_column()
