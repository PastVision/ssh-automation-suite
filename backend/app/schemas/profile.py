"""SSH profile API models.

Requests accept secrets on the way in. Responses report only whether a secret is
configured, plus non-reversible metadata. There is deliberately no response field
whose name or type could carry key material, a passphrase, or a sudo password.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class SecretStateRead(BaseModel):
    private_key_configured: bool
    private_key_fingerprint: str | None = None
    private_key_type: str | None = None
    private_key_comment: str | None = None
    passphrase_configured: bool
    sudo_password_configured: bool
    sudo_password_updated_at: str | None = None


class ProfileRead(BaseModel):
    user_id: int
    ssh_username: str
    default_port: int
    connect_timeout_seconds: int | None = None
    command_timeout_seconds: int | None = None
    keepalive_seconds: int | None = None
    secrets: SecretStateRead


class ProfileUpdate(BaseModel):
    """Ordinary settings only. Secrets have their own endpoints."""

    ssh_username: str | None = Field(default=None, max_length=120)
    default_port: int | None = Field(default=None, ge=1, le=65535)
    connect_timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    command_timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    keepalive_seconds: int | None = Field(default=None, ge=0, le=3600)
    clear_timeouts: bool = False

    @field_validator("ssh_username")
    @classmethod
    def _no_whitespace_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if any(character.isspace() for character in cleaned):
            raise ValueError("SSH username must not contain whitespace")
        if any(character in cleaned for character in (";", "|", "&", "$", "`", "\n", "\r")):
            raise ValueError("SSH username must not contain shell metacharacters")
        return cleaned


class PrivateKeyUpload(BaseModel):
    """The key is accepted as text, never as a filesystem path."""

    private_key: str = Field(min_length=1, max_length=100_000)
    passphrase: str | None = Field(default=None, max_length=1024)

    @field_validator("private_key")
    @classmethod
    def _looks_like_a_key(cls, value: str) -> str:
        if "PRIVATE KEY" not in value:
            raise ValueError("That does not look like a PEM private key")
        return value


class PassphraseUpdate(BaseModel):
    passphrase: str = Field(min_length=1, max_length=1024)


class SudoPasswordUpdate(BaseModel):
    """The primary sudo password. A separate credential from any Run-As password."""

    password: str = Field(min_length=1, max_length=1024)
