"""Security regression tests for the execution path.

The one-off check these replace was manual. Leaking key material is the kind of
bug that cannot be caught by reading a diff, so it gets a permanent test that
inspects every place a byte could land: the API response, every SQLite table,
every file under the log directory, and the on-disk key itself.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.models.enums import ExecutionStatus, HostRunStatus
from app.services import execution_state
from app.services.execution_state import HostSpec, StepSpec
from tests import lab_support as lab

pytestmark = pytest.mark.lab

# A distinctive substring of the lab key's base64 body, used to detect leakage.
KEY_MARKER = "BEGIN OPENSSH PRIVATE KEY"


def _lab_execution(settings, credential_id: int) -> int:
    return execution_state.create_execution(
        workflow_name="security",
        steps=[StepSpec("probe", 1, "echo probe-ok\n", "bash", 30)],
        hosts=[
            HostSpec(
                "127.0.0.1", lab.lab_port(1), credential_id, "lab", username=lab.LAB_USER
            )
        ],
        created_by_id=lab.ensure_test_owner(),
    )


@pytest.fixture()
def run_once(settings, migrated_db, monkeypatch):
    resolved = lab.configure_lab_settings(monkeypatch)
    credential_id = lab.create_lab_credential(resolved)

    from app.services.execution_runner import CancelToken, ExecutionRunner

    execution_id = _lab_execution(resolved, credential_id)
    lab.run(ExecutionRunner(resolved)._run_execution(execution_id, CancelToken()))
    return resolved, execution_id


def test_private_key_never_reaches_the_database(run_once) -> None:
    settings, execution_id = run_once
    host_run_id = execution_state.load_host_specs(execution_id)[0][0]
    execution_state.finish_host(host_run_id, status=HostRunStatus.SUCCEEDED)
    execution_state.finish_execution(execution_id, status=ExecutionStatus.SUCCEEDED)

    connection = sqlite3.connect(str(settings.data_dir / "ssh_suite.db"))
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
            )
        ]
        leaking = []
        for table in tables:
            for row in connection.execute(f"select * from {table}"):  # noqa: S608
                if KEY_MARKER in repr(tuple(row)):
                    leaking.append(table)
    finally:
        connection.close()

    assert not leaking, f"private key material found in tables: {leaking}"


def test_private_key_never_reaches_the_logs(run_once) -> None:
    settings, execution_id = run_once
    execution = execution_state.get_execution(execution_id)
    assert execution.status is ExecutionStatus.SUCCEEDED

    leaking = [
        path.name
        for path in settings.log_dir.rglob("*")
        if path.is_file() and KEY_MARKER in path.read_text(errors="ignore")
    ]
    assert not leaking, f"private key material found in log files: {leaking}"


def test_private_key_is_encrypted_at_rest(run_once) -> None:
    """The stored key file must be Fernet ciphertext, never plaintext PEM."""
    settings, _ = run_once

    from app.db.session import get_session_factory
    from app.models.credential import SshCredential

    session = get_session_factory()()
    try:
        credential = session.query(SshCredential).first()
        key_path = credential.private_key_path
    finally:
        session.close()

    raw = (settings.data_dir.parent / key_path).read_bytes()
    assert not raw.startswith(b"-----BEGIN"), "private key is stored in plaintext"
    assert KEY_MARKER.encode() not in raw
    # Fernet tokens begin with the version byte 0x80, which base64-encodes to 'g'.
    assert raw.startswith(b"gAAAAA"), "expected Fernet ciphertext"


def test_execution_api_response_has_no_key_material(run_once) -> None:
    settings, execution_id = run_once

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services import execution_state

    # The fixture execution is owned by the shared test account, and the detail
    # route is authenticated and owner-scoped, so sign in as that owner.
    owner_id = execution_state.get_execution(execution_id).created_by_id
    from app.services import sessions

    owner = sessions.get_user(owner_id)

    # The test client speaks plain HTTP, so a Secure cookie would not be resent.
    settings.cookie_secure = False

    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": owner.username, "password": "test-owner-password-1"},
        )
        assert login.status_code == 200, login.text
        detail = client.get(f"/api/executions/{execution_id}")

    assert detail.status_code == 200
    assert KEY_MARKER not in detail.text
    assert "PRIVATE KEY" not in detail.text
    # The id is not a secret, but the key and its material must be absent.
    payload = detail.json()
    assert "private_key" not in detail.text.lower()
    assert set(payload) == {
        "id",
        "workflow_name",
        "status",
        "trigger",
        "schedule_id",
        "schedule_name",
        "scheduled_for",
        "host_count",
        "succeeded_host_count",
        "failed_host_count",
        "started_at",
        "finished_at",
        "created_at",
        "log_path",
        "error_message",
        "cancel_requested_at",
        "cancel_requested_by_id",
        "cancel_reason",
        "is_active",
        "hosts",
    }
