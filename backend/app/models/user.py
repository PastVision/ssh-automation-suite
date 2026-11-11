"""Users and their authenticated sessions.

Identity comes from one of two sources (see ``app/services/auth.py``): a local
application account with a password, or a trusted authenticating proxy that
asserts an external identity for every request. ``external_id`` is the stable key
in ``proxy`` mode so a displayed name can change without orphaning history.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import timestamp_column


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Set only for proxy-authenticated users. Local accounts authenticate with a
    # password instead, and are denied login while ``is_active`` is false.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        """Name to show and to snapshot into audit records."""
        return self.display_name or self.username

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} username={self.username!r}>"


class UserSession(Base):
    """One logged-in browser session.

    Only the SHA-256 hash of the token is stored, so a database disclosure does
    not hand out usable sessions. ``csrf_token`` is a second, independent secret
    echoed through a non-HttpOnly cookie and a request header.
    """

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = timestamp_column()
    last_seen_at: Mapped[datetime] = timestamp_column()
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Recorded for the audit trail only; never used for authorization.
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_addr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_source: Mapped[str] = mapped_column(String(32), nullable=False, default="local")

    user: Mapped[User] = relationship(back_populates="sessions")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UserSession id={self.id} user_id={self.user_id}>"
