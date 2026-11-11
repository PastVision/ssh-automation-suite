"""Helpers for the real-SSH integration tests.

These tests connect to the running Docker test lab (``testlab/``), which exposes
each container on ``127.0.0.1:2201..2220`` via ``compose.loopback.yml``.

Starting the lab::

    cd testlab
    docker compose -f docker-compose.yml -f compose.loopback.yml up -d

The tests are skipped (not failed) when the lab is not reachable, so the ordinary
unit-test run stays fast. They are *not* mocked: every case performs a real SSH
connection to a real OpenSSH server. Set ``SSHSUITE_REQUIRE_LAB=1`` to turn a
missing lab into a failure instead of a skip, which is what CI should do so this
suite cannot silently rot.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
LAB_DIR = PROJECT_DIR / "testlab"
LAB_KEY = LAB_DIR / "keys" / "id_lab"
LAB_USER = "tester"
LAB_HOSTS = 20

# Registered in pytest.ini so the real-SSH tests can select themselves.
lab_marker = pytest.mark.lab


def lab_port(index: int) -> int:
    """Port published for the 1-based lab host index."""
    return 2200 + index


def lab_address(index: int) -> tuple[str, int]:
    return "127.0.0.1", lab_port(index)


def lab_hostname(index: int) -> str:
    """Container hostname the 1-based lab host reports to ``hostname``."""
    return f"ssh-host-{index:02d}"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def lab_available() -> bool:
    return _port_open("127.0.0.1", lab_port(1)) and LAB_KEY.exists()


def read_lab_key() -> str:
    return LAB_KEY.read_text(encoding="utf-8")


def configure_lab_settings(monkeypatch: pytest.MonkeyPatch):
    """Point the app at the lab and return the resolved settings.

    Host-key verification is disabled *only* for the lab, because those
    containers regenerate their host keys together with their volumes. Every other
    test asserts the verification default is on.
    """
    from app.core.config import get_settings
    from app.db import session as db_session
    from app.services import concurrency

    monkeypatch.setenv("SSHSUITE_SSH_ALLOW_UNKNOWN_HOST_KEYS", "true")
    get_settings.cache_clear()
    db_session.reset_engine()
    concurrency.reset_limiter()
    resolved = get_settings()
    resolved.ensure_directories()
    return resolved


def ensure_test_owner(username: str = "test-owner") -> int:
    """Idempotently create the account that owns fixtures in execution tests.
    ``ssh_credentials.owner_id`` is NOT NULL as of the multi-user migration, so a
    fixture credential needs an owner. Execution tests act as this user.
    """
    from app.services import sessions
    existing = sessions.get_user_by_username(username)
    if existing is not None:
        return existing.id
    return sessions.create_user(username, "test-owner-password-1").id
def create_lab_credential(settings, owner_id: int | None = None) -> int:
    """Store the lab keypair as an application credential and return its id.

    Mirrors what credential CRUD does: the encrypted key is written by
    ``key_store`` and the *path plus non-secret metadata* are recorded on the row.
    The private key itself is never stored in the database.
    """
    from app.db.session import get_session_factory
    from app.models.credential import SshCredential
    from app.services import key_store

    session = get_session_factory()()
    try:
        record = SshCredential(
            name="lab",
            username=LAB_USER,
            default_port=22,
            owner_id=owner_id if owner_id is not None else ensure_test_owner(),
        )
        session.add(record)
        session.flush()
        info = key_store.store_private_key(record.id, read_lab_key(), settings=settings)
        record.private_key_path = info.path
        record.private_key_fingerprint = info.fingerprint
        record.private_key_type = info.key_type
        record.private_key_comment = info.comment
        session.commit()
        return record.id
    finally:
        session.close()


def run(coro):
    """Run a coroutine in a fresh loop, as the ASGI server would."""
    return asyncio.run(coro)
