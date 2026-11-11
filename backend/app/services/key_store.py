"""Private key storage.

Keys are written to ``data/keys/<credential_id>.key`` as Fernet ciphertext with
owner-only permissions. The plaintext is never written to disk and never stored
in SQLite. Fingerprint, key type and comment are derived for display so the UI
can identify a key without exposing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import asyncssh

from app.core.config import Settings, get_settings
from app.core.security import decrypt_secret, encrypt_secret


class InvalidPrivateKeyError(ValueError):
    """Raised when the supplied text is not a usable private key."""


@dataclass(frozen=True)
class PrivateKeyInfo:
    path: str
    fingerprint: str
    key_type: str
    comment: str | None


def _key_path(credential_id: int, settings: Settings) -> Path:
    return settings.private_key_dir / f"{credential_id}.key"


def inspect_private_key(
    private_key_pem: str, passphrase: str | None = None
) -> asyncssh.SSHKey:
    """Parse the key locally (no network access) and return the key object."""
    try:
        return asyncssh.import_private_key(private_key_pem, passphrase=passphrase)
    except (asyncssh.KeyImportError, ValueError) as exc:
        raise InvalidPrivateKeyError(
            "The provided text is not a valid, unencrypted-or-passphrase-private SSH key."
        ) from exc


def store_private_key(
    credential_id: int,
    private_key_pem: str,
    passphrase: str | None = None,
    settings: Settings | None = None,
) -> PrivateKeyInfo:
    settings = settings or get_settings()
    key = inspect_private_key(private_key_pem, passphrase)

    settings.private_key_dir.mkdir(parents=True, exist_ok=True)
    path = _key_path(credential_id, settings)
    path.write_bytes(encrypt_secret(private_key_pem, settings).encode("ascii"))
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass

    return PrivateKeyInfo(
        path=settings.relative_path(path),
        fingerprint=key.get_fingerprint(),
        key_type=key.get_algorithm(),
        comment=key.get_comment(),
    )


def load_private_key(
    credential_id: int, settings: Settings | None = None
) -> str:
    settings = settings or get_settings()
    path = _key_path(credential_id, settings)
    if not path.exists():
        raise FileNotFoundError(f"No private key stored for credential {credential_id}")
    return decrypt_secret(path.read_bytes().decode("ascii"), settings).decode("utf-8")


def delete_private_key(credential_id: int, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    _key_path(credential_id, settings).unlink(missing_ok=True)
