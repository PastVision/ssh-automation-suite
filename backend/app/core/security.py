"""Symmetric encryption for secrets that must be recoverable at runtime.

SSH private keys have to be decrypted to open a connection, so they cannot be
hashed. They are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) under a key
stored outside the database in ``data/secret.key`` with owner-only permissions.
Losing that file makes stored credentials unusable; it must never be committed.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings, get_settings

_FERNET_CACHE: dict[str, Fernet] = {}

_UNUSABLE_KEY_MESSAGE = (
    "The encryption key file {path} exists but is not a usable Fernet key ({reason}). "
    "It will NOT be regenerated automatically: replacing it would make every "
    "credential already encrypted with the old key permanently unrecoverable. "
    "Restore the original file from backup, or delete it deliberately to start a "
    "fresh keystore and re-enter every SSH credential."
)


class DecryptionError(RuntimeError):
    """Raised when a ciphertext cannot be decrypted with the current key."""


class KeyFileError(RuntimeError):
    """Raised when an existing master key file is empty or malformed.

    Deliberately fatal: silently minting a replacement key would strand every
    credential encrypted under the original one.
    """


def _restrict_permissions(path: Path) -> None:
    """Best-effort owner-only permissions (POSIX mode is a no-op on Windows)."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - platform dependent
        pass


def load_or_create_key(path: Path) -> bytes:
    """Return the master key, generating one only when no key file exists.

    An existing file is authoritative. If it is empty or is not a valid Fernet
    key, this raises ``KeyFileError`` rather than overwriting it.
    """
    if path.exists():
        key = path.read_bytes().strip()
        if not key:
            raise KeyFileError(_UNUSABLE_KEY_MESSAGE.format(path=path, reason="file is empty"))
        try:
            Fernet(key)
        except (ValueError, TypeError) as exc:
            raise KeyFileError(
                _UNUSABLE_KEY_MESSAGE.format(path=path, reason="not a 32-byte url-safe key")
            ) from exc
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    _restrict_permissions(path)
    return key


def get_fernet(settings: Settings | None = None) -> Fernet:
    settings = settings or get_settings()
    key_path = settings.resolved_secret_key_file
    cached = _FERNET_CACHE.get(str(key_path))
    if cached is not None:
        return cached
    fernet = Fernet(load_or_create_key(key_path))
    _FERNET_CACHE[str(key_path)] = fernet
    return fernet


def reset_fernet_cache() -> None:
    _FERNET_CACHE.clear()


def encrypt_secret(plaintext: str | bytes, settings: Settings | None = None) -> str:
    raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    return get_fernet(settings).encrypt(raw).decode("ascii")


def decrypt_secret(token: str, settings: Settings | None = None) -> bytes:
    try:
        return get_fernet(settings).decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise DecryptionError(
            "Secret could not be decrypted with the configured key file."
        ) from exc


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def file_owner_only_readable(path: Path) -> bool:
    """Used by tests and diagnostics to assert the key is not world-readable."""
    if not path.exists():
        return False
    if os.name == "nt":
        return True
    return not path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
