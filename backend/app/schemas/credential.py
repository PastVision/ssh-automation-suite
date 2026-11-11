"""SSH credential API models.

``CredentialCreate`` is the only model with a private-key field. It is write-only
by construction: no read model declares key text or a passphrase, so no serializer
can echo one back. ``CredentialRead`` carries only the non-reversible metadata that
``key_store`` derives (fingerprint, algorithm, comment) plus a ``has_private_key``
flag, which is what the UI needs to render a usable row without a secret.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    default_port: int = Field(default=22, ge=1, le=65535)

    # Write-only. Encrypted by ``key_store`` before it touches disk, and never
    # stored in SQLite. The plaintext lives only for the duration of this request.
    private_key: str = Field(min_length=1, max_length=100_000)
    # Accepted only so it can be rejected explicitly rather than silently ignored.
    passphrase: str | None = Field(default=None, max_length=1024)

    @field_validator("name", "username")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("description")
    @classmethod
    def _blank_description_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("private_key")
    @classmethod
    def _key_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("private_key must not be empty")
        return value


class CredentialRead(BaseModel):
    """A credential as returned to a client. Never includes key material."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    username: str
    description: str | None = None
    default_port: int
    private_key_fingerprint: str | None = None
    private_key_type: str | None = None
    private_key_comment: str | None = None
    # True when a key is stored on disk. The key itself is never serialisable.
    has_private_key: bool = False
    created_at: datetime
    updated_at: datetime


class CredentialListResponse(BaseModel):
    items: list[CredentialRead]


class CredentialDeleteBlocked(BaseModel):
    """Returned as the 409 detail when a credential is still in use."""

    message: str
    workflows: int
    active_executions: int
