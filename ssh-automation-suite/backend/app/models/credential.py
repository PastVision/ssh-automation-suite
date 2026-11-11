"""SSH credentials.

The private key is never stored in this table. Only an encrypted copy on disk
(``data/keys/<id>.key``) and non-reversible metadata (fingerprint, comment,
key type) live alongside the connection settings.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import timestamp_column


class SshCredential(Base):
    __tablename__ = "ssh_credentials"
    __table_args__ = (
        # Credentials are private to their owner, so names are unique per user
        # rather than globally: two users may each have a "prod-bastion".
        UniqueConstraint("owner_id", "name", name="uq_ssh_credentials_owner_id_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Path to the Fernet ciphertext, relative to the backend directory.
    private_key_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    private_key_fingerprint: Mapped[str | None] = mapped_column(String(120), nullable=True)
    private_key_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Safe to display; never the key itself.
    private_key_comment: Mapped[str | None] = mapped_column(String(255), nullable=True)

    default_port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    # Encrypted passphrase for a passphrase-protected key.
    passphrase_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SshCredential id={self.id} name={self.name!r}>"
