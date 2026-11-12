"""SSH credential HTTP surface: ownership, secrecy, validation, delete guards.

These drive the real API against a migrated database, so they cover the layers a
service test cannot: status codes, request validation, and above all that no
secret can appear in a response.

The key material used here is a freshly generated ed25519 key rather than the
Docker lab's key, because these tests must run without the lab.
"""

from __future__ import annotations

import asyncssh
import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")
BOB = ("bob", "bob-password-1")


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    key = asyncssh.generate_private_key("ssh-ed25519")
    return key.export_private_key().decode()


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


@pytest.fixture()
def bob(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *BOB)


def _create(
    client: TestClient,
    token: str,
    private_key_pem: str,
    *,
    name: str = "lab",
    username: str = "tester",
    passphrase: str | None = None,
) -> dict:
    payload = {
        "name": name,
        "username": username,
        "private_key": private_key_pem,
        "default_port": 22,
    }
    if passphrase is not None:
        payload["passphrase"] = passphrase
    return client.post("/api/credentials", json=payload, headers=csrf(token))


# --------------------------------------------------------------------------- #
# Create + list
# --------------------------------------------------------------------------- #


def test_create_then_list_returns_metadata_and_never_the_key(
    alice, private_key_pem: str
) -> None:
    client, token = alice
    created = _create(client, token, private_key_pem)
    assert created.status_code == 201, created.text

    body = created.json()
    assert body["name"] == "lab"
    assert body["username"] == "tester"
    assert body["has_private_key"] is True
    # The key is parsed for metadata, which is what identifies it in the UI.
    assert body["private_key_fingerprint"]
    assert body["private_key_type"]
    assert "SHA256:" in body["private_key_fingerprint"]

    # No response field may carry key text or a passphrase.
    assert "private_key" not in body
    assert "passphrase" not in body
    assert "PRIVATE KEY" not in created.text

    listed = client.get("/api/credentials", headers=csrf(token))
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["id"] for item in items] == [body["id"]]
    assert "PRIVATE KEY" not in listed.text


def test_the_private_key_is_encrypted_on_disk(
    alice, private_key_pem: str, migrated_db
) -> None:
    """The stored file must be ciphertext, not the key we supplied."""
    client, token = alice
    created = _create(client, token, private_key_pem)
    assert created.status_code == 201, created.text
    credential_id = created.json()["id"]

    from app.core.config import get_settings
    from app.services import key_store

    path = get_settings().private_key_dir / f"{credential_id}.key"
    assert path.exists()
    raw = path.read_text(encoding="ascii")
    assert "PRIVATE KEY" not in raw
    assert private_key_pem.strip() not in raw

    # ...but it round-trips back to the original through key_store.
    assert key_store.load_private_key(credential_id) == private_key_pem


def test_the_key_is_not_stored_in_sqlite(
    alice, private_key_pem: str, migrated_db
) -> None:
    client, token = alice
    created = _create(client, token, private_key_pem)
    assert created.status_code == 201, created.text

    # Search every text column of the credential row for key material.
    from app.db.session import get_session_factory
    from app.models.credential import SshCredential

    session = get_session_factory()()
    try:
        row = session.get(SshCredential, created.json()["id"])
        blob = " ".join(
            str(getattr(row, column.name))
            for column in SshCredential.__table__.columns
        )
    finally:
        session.close()

    assert "PRIVATE KEY" not in blob
    assert private_key_pem.strip() not in blob


def test_a_passphrase_is_rejected_rather_than_stored_unused(
    alice, private_key_pem: str
) -> None:
    """The runner loads keys with no passphrase, so accepting one would be a trap.

    Storing an unusable secret is worse than refusing it: the credential would be
    accepted, then fail to connect with a misleading parse error.
    """
    client, token = alice
    response = _create(client, token, private_key_pem, passphrase="hunter2")
    assert response.status_code == 400, response.text
    assert "passphrase" in response.json()["detail"].lower()

    # Nothing was persisted.
    assert client.get("/api/credentials", headers=csrf(token)).json()["items"] == []


def test_a_passphrase_encrypted_key_is_rejected(
    alice, private_key_pem: str
) -> None:
    """An encrypted key cannot be parsed without its passphrase, so it is refused."""
    try:
        encrypted = (
            asyncssh.import_private_key(private_key_pem)
            .export_private_key(passphrase="hunter2")
            .decode()
        )
    except Exception:  # pragma: no cover - depends on bcrypt support
        pytest.skip("asyncssh build cannot export an encrypted OpenSSH key")

    client, token = alice
    response = _create(client, token, encrypted)
    assert response.status_code == 400, response.text
    assert "could not be read" in response.json()["detail"].lower()


def test_an_unusable_key_text_is_rejected_without_persisting(
    alice,
) -> None:
    client, token = alice
    response = _create(client, token, "this is not a key\n")
    assert response.status_code == 400, response.text
    assert "could not be read" in response.json()["detail"].lower()
    assert client.get("/api/credentials", headers=csrf(token)).json()["items"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "   "),
        ("username", ""),
        ("default_port", 0),
        ("default_port", 70000),
    ],
)
def test_invalid_fields_are_rejected(
    alice, private_key_pem: str, field: str, value
) -> None:
    client, token = alice
    payload = {
        "name": "ok",
        "username": "ok",
        "private_key": private_key_pem,
        "default_port": 22,
    }
    payload[field] = value
    response = client.post("/api/credentials", json=payload, headers=csrf(token))
    assert response.status_code == 422, response.text


def test_duplicate_names_are_refused_per_user(
    alice, private_key_pem: str
) -> None:
    client, token = alice
    assert _create(client, token, private_key_pem).status_code == 201
    duplicate = _create(client, token, private_key_pem, name="LAB")
    assert duplicate.status_code == 409, duplicate.text
    assert "already have a credential" in duplicate.json()["detail"].lower()


def test_credentials_require_authentication(app_client, private_key_pem: str) -> None:
    response = app_client.post(
        "/api/credentials",
        json={
            "name": "x",
            "username": "x",
            "private_key": private_key_pem,
            "default_port": 22,
        },
    )
    assert response.status_code == 401
    assert app_client.get("/api/credentials").status_code == 401


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_credentials_are_private_to_their_owner(
    alice, bob, private_key_pem: str
) -> None:
    alice_client, alice_token = alice
    bob_client, bob_token = bob

    created = _create(alice_client, alice_token, private_key_pem)
    assert created.status_code == 201
    credential_id = created.json()["id"]

    # Bob's list is empty: Alice's credential is invisible, not merely undeletable.
    assert bob_client.get("/api/credentials", headers=csrf(bob_token)).json()["items"] == []

    # Deleting somebody else's credential answers 404, so ids cannot be probed.
    attempt = bob_client.delete(
        f"/api/credentials/{credential_id}", headers=csrf(bob_token)
    )
    assert attempt.status_code == 404

    # Alice's credential survived Bob's attempt.
    still_there = alice_client.get("/api/credentials", headers=csrf(alice_token))
    assert [item["id"] for item in still_there.json()["items"]] == [credential_id]


def test_a_workflow_cannot_reference_another_users_credential(
    alice, bob, private_key_pem: str
) -> None:
    alice_client, alice_token = alice
    bob_client, bob_token = bob

    credential_id = _create(alice_client, alice_token, private_key_pem).json()["id"]

    # Bob points his own workflow at Alice's credential id, using his *own*
    # template so the credential is the only thing wrong with the request. It must
    # be refused, and refused in a way that does not confirm Alice's id exists.
    template = bob_client.post(
        "/api/templates",
        json={
            "name": "bob-t",
            "visibility": "private",
            "steps": [{"name": "s1", "script": "echo hi\n", "interpreter": "bash"}],
        },
        headers=csrf(bob_token),
    )
    assert template.status_code == 201, template.text

    created = bob_client.post(
        "/api/workflows",
        json={
            "name": "bob-wf",
            "credential_id": credential_id,
            "entries": [{"kind": "template", "template_id": template.json()["id"]}],
            "hosts": [{"hostname": "web-1.example.com"}],
            "variables": [],
        },
        headers=csrf(bob_token),
    )
    assert created.status_code == 422, created.text
    # "does not exist" rather than "not yours": the other user's id is not confirmed.
    assert "does not exist" in created.json()["detail"].lower()
    assert "bob-wf" not in str(
        bob_client.get("/api/workflows", headers=csrf(bob_token)).json()
    )


# --------------------------------------------------------------------------- #
# Delete safeguards
# --------------------------------------------------------------------------- #


def test_delete_removes_the_row_and_the_key_file(
    alice, private_key_pem: str
) -> None:
    client, token = alice
    credential_id = _create(client, token, private_key_pem).json()["id"]

    from app.core.config import get_settings

    path = get_settings().private_key_dir / f"{credential_id}.key"
    assert path.exists()

    deleted = client.delete(f"/api/credentials/{credential_id}", headers=csrf(token))
    assert deleted.status_code == 204, deleted.text
    assert not path.exists(), "the ciphertext must not outlive the row"
    assert client.get("/api/credentials", headers=csrf(token)).json()["items"] == []


def test_delete_is_blocked_while_a_workflow_uses_the_credential(
    alice, private_key_pem: str
) -> None:
    """Deleting would leave the workflow unrunnable and orphan its FK."""
    client, token = alice
    credential_id = _create(client, token, private_key_pem).json()["id"]

    template = client.post(
        "/api/templates",
        json={
            "name": "t",
            "visibility": "private",
            "steps": [{"name": "s1", "script": "echo hi\n", "interpreter": "bash"}],
        },
        headers=csrf(token),
    )
    assert template.status_code == 201, template.text

    workflow = client.post(
        "/api/workflows",
        json={
            "name": "wf",
            "credential_id": credential_id,
            "entries": [{"kind": "template", "template_id": template.json()["id"]}],
            "hosts": [{"hostname": "web-1.example.com"}],
            "variables": [],
        },
        headers=csrf(token),
    )
    assert workflow.status_code == 201, workflow.text

    blocked = client.delete(f"/api/credentials/{credential_id}", headers=csrf(token))
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["workflows"] == 1
    assert detail["active_executions"] == 0
    assert "workflow" in detail["message"].lower()

    # The credential and its key file are untouched.
    from app.core.config import get_settings

    assert (get_settings().private_key_dir / f"{credential_id}.key").exists()
    assert len(client.get("/api/credentials", headers=csrf(token)).json()["items"]) == 1

    # Once the workflow is gone the credential can be deleted.
    removed = client.delete(
        f"/api/workflows/{workflow.json()['id']}", headers=csrf(token)
    )
    assert removed.status_code == 204, removed.text
    assert (
        client.delete(f"/api/credentials/{credential_id}", headers=csrf(token)).status_code
        == 204
    )


def test_delete_is_blocked_while_an_execution_is_active(
    alice, private_key_pem: str, migrated_db
) -> None:
    """A run in flight would lose its key mid-execution."""
    client, token = alice
    credential_id = _create(client, token, private_key_pem).json()["id"]

    template = client.post(
        "/api/templates",
        json={
            "name": "t",
            "visibility": "private",
            "steps": [{"name": "s1", "script": "echo hi\n", "interpreter": "bash"}],
        },
        headers=csrf(token),
    )
    assert template.status_code == 201, template.text

    workflow = client.post(
        "/api/workflows",
        json={
            "name": "wf",
            "credential_id": credential_id,
            "entries": [{"kind": "template", "template_id": template.json()["id"]}],
            "hosts": [{"hostname": "web-1.example.com"}],
            "variables": [],
        },
        headers=csrf(token),
    )
    assert workflow.status_code == 201, workflow.text

    # A workflow id alone cannot be deleted once an execution references it, so
    # record the run without a workflow link to keep the reference checks honest.
    from app.db.session import get_session_factory
    from app.models.enums import ExecutionStatus
    from app.models.execution import Execution

    session = get_session_factory()()
    try:
        session.add(
            Execution(
                workflow_id=workflow.json()["id"],
                workflow_name="wf",
                status=ExecutionStatus.RUNNING,
                trigger="manual",
                host_count=1,
            )
        )
        session.commit()
    finally:
        session.close()

    blocked = client.delete(f"/api/credentials/{credential_id}", headers=csrf(token))
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["active_executions"] == 1
    assert "running" in detail["message"].lower()


def test_deleting_a_missing_credential_is_a_404(alice) -> None:
    client, token = alice
    response = client.delete("/api/credentials/999999", headers=csrf(token))
    assert response.status_code == 404


def test_rejection_does_not_log_the_key(
    alice, private_key_pem: str, caplog
) -> None:
    """A validation failure must not echo the secret into the log."""
    client, token = alice
    secret_marker = "SUPERSECRETKEYMATERIAL"
    caplog.set_level(10)
    response = _create(client, token, secret_marker)
    assert response.status_code == 400
    assert secret_marker not in caplog.text
