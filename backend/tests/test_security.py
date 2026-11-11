"""Private keys must be encrypted at rest, never in SQLite, never plaintext."""

from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken

from app.core.security import (
    DecryptionError,
    KeyFileError,
    decrypt_secret,
    encrypt_secret,
    get_fernet,
    load_or_create_key,
    mask_secret,
)
from app.services.key_store import (
    InvalidPrivateKeyError,
    delete_private_key,
    inspect_private_key,
    load_private_key,
    store_private_key,
)


def test_secret_roundtrip() -> None:
    token = encrypt_secret("super-secret")
    assert isinstance(token, str)
    assert "super-secret" not in token
    assert decrypt_secret(token) == b"super-secret"


def test_each_encryption_is_unique() -> None:
    assert encrypt_secret("same") != encrypt_secret("same")


def test_decryption_with_wrong_key_fails(settings) -> None:
    from cryptography.fernet import Fernet

    token = encrypt_secret("value")
    with pytest.raises(InvalidToken):
        Fernet(Fernet.generate_key()).decrypt(token.encode("ascii"))


def test_corrupt_ciphertext_raises_clear_error() -> None:
    with pytest.raises(DecryptionError):
        decrypt_secret("not-a-valid-token")


def test_key_file_is_created_once_and_reused(settings) -> None:
    path = settings.resolved_secret_key_file
    first = get_fernet(settings)
    assert path.exists()

    from app.core import security

    security.reset_fernet_cache()
    second = get_fernet(settings)

    assert first.encrypt(b"x") != second.encrypt(b"x")
    assert decrypted_same(first, second)


def decrypted_same(first, second) -> bool:
    token = first.encrypt(b"value").decode()
    return second.decrypt(token) == b"value"


def test_key_file_is_not_world_readable(settings) -> None:
    get_fernet(settings)
    path = settings.resolved_secret_key_file

    import os
    import stat

    if os.name == "nt":
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    assert not path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_mask_secret_keeps_only_tail() -> None:
    assert mask_secret("abcdefgh", visible=4) == "****efgh"
    assert mask_secret("ab") == "**"
    assert mask_secret("") == ""


def test_missing_key_file_is_generated(settings) -> None:
    path = settings.resolved_secret_key_file
    assert not path.exists()

    key = load_or_create_key(path)

    assert path.exists()
    assert path.read_bytes().strip() == key
    assert len(key) == 44  # 32 url-safe base64 bytes


def test_empty_key_file_fails_loudly_and_is_not_overwritten(settings) -> None:
    path = settings.resolved_secret_key_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    with pytest.raises(KeyFileError) as excinfo:
        load_or_create_key(path)

    message = str(excinfo.value)
    assert "exists but is not a usable Fernet key" in message
    assert "empty" in message
    # The operator must be told the consequence, and that we did not silently rotate.
    assert "unrecoverable" in message
    assert "NOT be regenerated" in message
    # The original file must be left exactly as found.
    assert path.read_bytes() == b""


def test_whitespace_only_key_file_fails_loudly(settings) -> None:
    path = settings.resolved_secret_key_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"  \n\t \n")

    with pytest.raises(KeyFileError):
        load_or_create_key(path)

    assert path.read_bytes() == b"  \n\t \n"


@pytest.mark.parametrize(
    "payload",
    [
        b"not-a-fernet-key",  # not base64 at all
        b"deadbeef",  # right alphabet, wrong length
        b"MDEyMzQ1Njc4OWFiY2RlZg==",  # valid base64, but only 16 raw bytes
        b"!" * 44,  # right length, illegal alphabet
        b"-----BEGIN FERNET KEY-----\nredacted\n",  # wrong format entirely
    ],
)
def test_invalid_key_file_fails_loudly(settings, payload: bytes) -> None:
    path = settings.resolved_secret_key_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    with pytest.raises(KeyFileError) as excinfo:
        load_or_create_key(path)

    assert "not a 32-byte url-safe key" in str(excinfo.value)
    assert path.read_bytes() == payload


def test_invalid_key_file_is_not_silently_replaced_via_get_fernet(settings) -> None:
    """get_fernet must surface the integrity failure, not mint a new key."""
    from app.core import security

    path = settings.resolved_secret_key_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    security.reset_fernet_cache()
    with pytest.raises(KeyFileError):
        get_fernet(settings)

    assert path.read_bytes() == b""
    assert str(path) not in security._FERNET_CACHE


def test_old_ciphertext_is_unreadable_with_a_replaced_key(settings) -> None:
    """Documents the data loss the fatal error is preventing."""
    token = encrypt_secret("credential-material", settings)

    # Simulate a deliberate, operator-driven keystore reset.
    settings.resolved_secret_key_file.unlink()
    from app.core import security

    security.reset_fernet_cache()
    encrypt_secret("something-new", settings)

    with pytest.raises(DecryptionError):
        decrypt_secret(token, settings)


def test_invalid_private_key_is_rejected() -> None:
    with pytest.raises(InvalidPrivateKeyError):
        inspect_private_key("this is not a key")


def test_private_key_is_encrypted_on_disk(settings) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    info = store_private_key(1, pem, settings=settings)

    stored_path = settings.private_key_dir / "1.key"
    raw = stored_path.read_bytes()

    assert stored_path.exists()
    assert b"OPENSSH PRIVATE KEY" not in raw
    assert b"BEGIN" not in raw
    assert info.key_type == "ssh-ed25519"
    assert info.fingerprint.startswith("SHA256:")
    assert info.path == settings.relative_path(stored_path)

    assert load_private_key(1, settings=settings) == pem


def test_delete_private_key_removes_file(settings) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    pem = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    store_private_key(2, pem, settings=settings)
    delete_private_key(2, settings=settings)

    assert not (settings.private_key_dir / "2.key").exists()
