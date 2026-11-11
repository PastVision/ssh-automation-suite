"""Password hashing and session-token generation.

Passwords use scrypt from ``cryptography`` (already a dependency), so no new
package is introduced. Session tokens are 256 bits of CSPRNG output: they are
stored only as a SHA-256 digest, which is sufficient because the token itself is
already unguessable -- a slow KDF would add login latency for no security gain.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# 2**14 * 8 * 128 = 16 MiB per hash. Deliberately modest so a shared server with
# a handful of logins stays responsive; raise n alongside deployment sizing.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16

_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    """Return a self-describing, salted scrypt hash.

    The parameters travel with the digest so they can be raised later without
    invalidating existing passwords.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _derive(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return "$".join(
        (
            _PREFIX,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str | None) -> bool:
    """Constant-time check. Returns False for any malformed stored value."""
    if not password or not encoded:
        return False
    try:
        prefix, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if prefix != _PREFIX:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        derived = _derive(password, salt, int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(derived, expected)


def needs_rehash(encoded: str | None) -> bool:
    """True when a stored hash used weaker parameters than the current policy."""
    if not encoded:
        return True
    try:
        prefix, n, r, p, _salt, _hash = encoded.split("$")
    except ValueError:
        return True
    return (prefix, int(n), int(r), int(p)) != (_PREFIX, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


def _derive(password: str, salt: bytes, n: int, r: int, p: int, length: int = _DKLEN) -> bytes:
    kdf = Scrypt(salt=salt, length=length, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def generate_session_token() -> str:
    """A new opaque session token. Only its digest is ever persisted."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """SHA-256 hex digest used as the database lookup key for a session."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    """Alias kept for symmetry; session tokens and CSRF tokens are both opaque."""
    return hash_session_token(token)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)
