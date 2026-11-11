"""Per-user SSH profile management.

The plaintext of a private key, its passphrase, or the sudo password exists only
inside this module and only for the duration of a call. Stored forms are:

* private key -> Fernet ciphertext on disk at ``data/keys/user-<id>.key``
* passphrase  -> Fernet ciphertext in ``user_ssh_secrets.key_passphrase_encrypted``
* sudo password -> Fernet ciphertext in ``user_ssh_secrets.sudo_password_encrypted``

No function here returns a secret. Read paths return ``SecretState`` booleans and
safe metadata (fingerprint, algorithm) so the UI can say "configured" without ever
receiving the value.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.core.security import decrypt_secret, encrypt_secret
from app.db.session import get_session_factory
from app.db.types import utcnow
from app.models.profile import UserSshProfile, UserSshSecret
from app.services import key_store

logger = logging.getLogger(__name__)

SUDO_TIMEOUT_SECONDS = 15


class ProfileNotFound(LookupError):
    pass


@dataclass(frozen=True)
class SecretState:
    """What the UI is allowed to know about stored secrets."""

    private_key_configured: bool
    private_key_fingerprint: str | None
    private_key_type: str | None
    private_key_comment: str | None
    passphrase_configured: bool
    sudo_password_configured: bool
    sudo_password_updated_at: str | None


@dataclass(frozen=True)
class ProfileView:
    user_id: int
    ssh_username: str
    default_port: int
    connect_timeout_seconds: int | None
    command_timeout_seconds: int | None
    keepalive_seconds: int | None
    secrets: SecretState


def _key_path(user_id: int, settings: Settings) -> Path:
    return settings.private_key_dir / f"user-{user_id}.key"


def get_or_create_profile(user_id: int, *, settings: Settings | None = None) -> UserSshProfile:
    """Every user has a primary profile; it is created empty on first access.

    Two concurrent first requests, or a secret being set before the profile is ever
    read, can both try to insert these rows. Each insert therefore runs in its own
    savepoint and a unique-constraint loss is resolved by re-reading the winner's
    row. Checking first and inserting later would not be atomic in SQLite.
    """
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        profile = _ensure_profile_row(session, user_id)
        _ensure_secret_row(session, user_id)
        return profile
    finally:
        session.close()


def _ensure_profile_row(session, user_id: int) -> UserSshProfile:
    existing = session.scalar(
        select(UserSshProfile).where(UserSshProfile.user_id == user_id)
    )
    if existing is not None:
        return existing

    try:
        with session.begin_nested():
            profile = UserSshProfile(user_id=user_id, ssh_username="", default_port=22)
            session.add(profile)
    except IntegrityError:
        # Another request created it first; that row is authoritative.
        profile = session.scalar(
            select(UserSshProfile).where(UserSshProfile.user_id == user_id)
        )
        if profile is None:  # pragma: no cover - a different constraint failed
            raise
        return profile

    session.commit()
    session.refresh(profile)
    return profile


def _ensure_secret_row(session, user_id: int) -> UserSshSecret:
    row = session.scalar(select(UserSshSecret).where(UserSshSecret.user_id == user_id))
    if row is not None:
        return row

    try:
        with session.begin_nested():
            row = UserSshSecret(user_id=user_id)
            session.add(row)
    except IntegrityError:
        row = session.scalar(select(UserSshSecret).where(UserSshSecret.user_id == user_id))
        if row is None:  # pragma: no cover - a different constraint failed
            raise

    session.commit()
    session.refresh(row)
    return row


def secret_state(user_id: int, *, settings: Settings | None = None) -> SecretState:
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        row = session.scalar(select(UserSshSecret).where(UserSshSecret.user_id == user_id))
        if row is None:
            return SecretState(False, None, None, None, False, False, None)
        return SecretState(
            private_key_configured=(
                bool(row.private_key_path) and _key_path(user_id, settings).exists()
            ),
            private_key_fingerprint=row.private_key_fingerprint,
            private_key_type=row.private_key_type,
            private_key_comment=row.private_key_comment,
            passphrase_configured=bool(row.key_passphrase_encrypted),
            sudo_password_configured=bool(row.sudo_password_encrypted),
            sudo_password_updated_at=(
                row.sudo_password_updated_at.isoformat() if row.sudo_password_updated_at else None
            ),
        )
    finally:
        session.close()


def view(user_id: int, *, settings: Settings | None = None) -> ProfileView:
    settings = settings or get_settings()
    profile = get_or_create_profile(user_id, settings=settings)
    return ProfileView(
        user_id=user_id,
        ssh_username=profile.ssh_username,
        default_port=profile.default_port,
        connect_timeout_seconds=profile.connect_timeout_seconds,
        command_timeout_seconds=profile.command_timeout_seconds,
        keepalive_seconds=profile.keepalive_seconds,
        secrets=secret_state(user_id, settings=settings),
    )


def update_settings(
    user_id: int,
    *,
    ssh_username: str | None = None,
    default_port: int | None = None,
    connect_timeout_seconds: int | None = None,
    command_timeout_seconds: int | None = None,
    keepalive_seconds: int | None = None,
    clear_timeouts: bool = False,
    settings: Settings | None = None,
) -> ProfileView:
    """Update ordinary (non-secret) settings. Only provided fields change."""
    settings = settings or get_settings()
    get_or_create_profile(user_id, settings=settings)
    session = get_session_factory()()
    try:
        profile = session.scalar(
            select(UserSshProfile).where(UserSshProfile.user_id == user_id)
        )
        if profile is None:  # pragma: no cover - created just above
            raise ProfileNotFound(str(user_id))
        if ssh_username is not None:
            profile.ssh_username = ssh_username
        if default_port is not None:
            profile.default_port = default_port
        if clear_timeouts:
            profile.connect_timeout_seconds = None
            profile.command_timeout_seconds = None
            profile.keepalive_seconds = None
        if connect_timeout_seconds is not None:
            profile.connect_timeout_seconds = connect_timeout_seconds
        if command_timeout_seconds is not None:
            profile.command_timeout_seconds = command_timeout_seconds
        if keepalive_seconds is not None:
            profile.keepalive_seconds = keepalive_seconds
        session.commit()
    finally:
        session.close()
    return view(user_id, settings=settings)


def set_private_key(
    user_id: int,
    private_key_pem: str,
    passphrase: str | None = None,
    *,
    settings: Settings | None = None,
) -> SecretState:
    """Validate, encrypt and store a private key.

    The key is parsed locally first: an unusable key is rejected before anything is
    written, so a bad upload cannot leave a half-configured profile.
    """
    settings = settings or get_settings()
    info = key_store.inspect_private_key(private_key_pem, passphrase)

    settings.private_key_dir.mkdir(parents=True, exist_ok=True)
    path = _key_path(user_id, settings)
    path.write_bytes(encrypt_secret(private_key_pem, settings).encode("ascii"))
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass

    session = get_session_factory()()
    try:
        row = _ensure_secret_row(session, user_id)
        row.private_key_path = settings.relative_path(path)
        row.private_key_fingerprint = info.get_fingerprint()
        row.private_key_type = info.get_algorithm()
        row.private_key_comment = info.get_comment()
        if passphrase is not None:
            row.key_passphrase_encrypted = encrypt_secret(passphrase, settings)
        session.commit()
    finally:
        session.close()
    return secret_state(user_id, settings=settings)


def clear_private_key(user_id: int, *, settings: Settings | None = None) -> SecretState:
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        row = session.scalar(select(UserSshSecret).where(UserSshSecret.user_id == user_id))
        if row is not None:
            row.private_key_path = None
            row.private_key_fingerprint = None
            row.private_key_type = None
            row.private_key_comment = None
            row.key_passphrase_encrypted = None
            session.commit()
    finally:
        session.close()
    _key_path(user_id, settings).unlink(missing_ok=True)
    return secret_state(user_id, settings=settings)


def set_passphrase(
    user_id: int, passphrase: str, *, settings: Settings | None = None
) -> SecretState:
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        row = _ensure_secret_row(session, user_id)
        row.key_passphrase_encrypted = encrypt_secret(passphrase, settings)
        session.commit()
    finally:
        session.close()
    return secret_state(user_id, settings=settings)


def clear_passphrase(user_id: int, *, settings: Settings | None = None) -> SecretState:
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        row = session.scalar(select(UserSshSecret).where(UserSshSecret.user_id == user_id))
        if row is not None:
            row.key_passphrase_encrypted = None
            session.commit()
    finally:
        session.close()
    return secret_state(user_id, settings=settings)


def set_sudo_password(
    user_id: int, password: str, *, settings: Settings | None = None
) -> SecretState:
    """Store the primary sudo password (encrypted).

    Kept in its own column, separate from any future Run-As credential, so the two
    can never be confused or accidentally reused for each other.
    """
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        row = _ensure_secret_row(session, user_id)
        row.sudo_password_encrypted = encrypt_secret(password, settings)
        row.sudo_password_updated_at = utcnow()
        session.commit()
    finally:
        session.close()
    return secret_state(user_id, settings=settings)


def clear_sudo_password(user_id: int, *, settings: Settings | None = None) -> SecretState:
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        row = session.scalar(select(UserSshSecret).where(UserSshSecret.user_id == user_id))
        if row is not None:
            row.sudo_password_encrypted = None
            row.sudo_password_updated_at = None
            session.commit()
    finally:
        session.close()
    return secret_state(user_id, settings=settings)


# --------------------------------------------------------------------------- #
# Runtime access (decryption)
# --------------------------------------------------------------------------- #
# These are the only functions that hand back plaintext. They are called by the
# SSH execution path, never by an API layer, and their return values must never
# be logged or placed in a response.

def load_private_key_pem(user_id: int, *, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    path = _key_path(user_id, settings)
    if not path.exists():
        raise FileNotFoundError(f"No private key stored for user {user_id}")
    return decrypt_secret(path.read_bytes().decode("ascii"), settings).decode("utf-8")


def load_passphrase(user_id: int, *, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        row = session.scalar(select(UserSshSecret).where(UserSshSecret.user_id == user_id))
        if row is None or not row.key_passphrase_encrypted:
            return None
        return decrypt_secret(row.key_passphrase_encrypted, settings).decode("utf-8")
    finally:
        session.close()


def build_client_key(user_id: int, *, settings: Settings | None = None):
    """Return an ``asyncssh.SSHKey`` for this user's profile."""
    settings = settings or get_settings()
    pem = load_private_key_pem(user_id, settings=settings)
    passphrase = load_passphrase(user_id, settings=settings)
    return key_store.inspect_private_key(pem, passphrase)


def load_sudo_password(user_id: int, *, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    session = get_session_factory()()
    try:
        row = session.scalar(select(UserSshSecret).where(UserSshSecret.user_id == user_id))
        if row is None or not row.sudo_password_encrypted:
            return None
        return decrypt_secret(row.sudo_password_encrypted, settings).decode("utf-8")
    finally:
        session.close()
