"""Per-user SSH configuration.

Two tables on purpose. ``user_ssh_profiles`` holds ordinary, displayable settings.
``user_ssh_secrets`` holds only Fernet ciphertext, one row per user, with no
relationship loaded by default, so listing profiles can never accidentally
serialise a secret. The private key lives on disk (``data/keys/user-<id>.key``)
exactly like the credential keystore, because it has to be recoverable at runtime.

The primary sudo password is a *separate* secret from the future Run-As password.
They are distinct columns and distinct credentials; do not merge them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import timestamp_column


class UserSshProfile(Base):
    """A user's primary SSH configuration (non-secret fields)."""

    __tablename__ = "user_ssh_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    ssh_username: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    default_port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)

    # Connection preferences. ``None`` means "use the application default", so a
    # user who never tunes these follows the deployment configuration.
    connect_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keepalive_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UserSshProfile user_id={self.user_id}>"


class UserSshSecret(Base):
    """Encrypted material for a user's SSH profile.

    Columns are ciphertext from ``app.core.security``. Nothing here is ever
    returned by an API; endpoints report ``configured`` booleans and safe
    metadata (fingerprint, key type) only.
    """

    __tablename__ = "user_ssh_secrets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Path to the Fernet ciphertext of the private key, relative to BACKEND_DIR.
    private_key_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    private_key_fingerprint: Mapped[str | None] = mapped_column(String(120), nullable=True)
    private_key_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    private_key_comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    key_passphrase_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Sudo password for the primary SSH user. Distinct from Run-As credentials.
    sudo_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    sudo_password_updated_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UserSshSecret user_id={self.user_id}>"
