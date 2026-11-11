"""Multi-user isolation: profiles, credentials, executions, SSH secrets.

The theme is that one user must never observe or affect another's data. Every test
here uses two real accounts in separate cookie jars.

SSH secrets get the most attention: they must be encrypted at rest, must never
appear in any response, and the primary sudo password must be a distinct secret
from any future Run-As credential.
"""

from __future__ import annotations

import asyncssh
from fastapi.testclient import TestClient

from app.db.session import get_session_factory
from tests.conftest import csrf, sign_in

PASSWORD = "alice-password-1"


def _test_key() -> str:
    return asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()


# --------------------------------------------------------------------------- #
# Profiles are per-user
# --------------------------------------------------------------------------- #

def test_profile_is_created_empty_for_a_new_user(app_client: TestClient, users) -> None:
    sign_in(app_client, "alice", PASSWORD)
    body = app_client.get("/api/profile").json()

    assert body["user_id"] == users["alice"]
    assert body["ssh_username"] == ""
    assert body["default_port"] == 22
    assert body["secrets"]["private_key_configured"] is False


def test_each_user_has_an_independent_profile(app_client: TestClient, users) -> None:
    alice_token = sign_in(app_client, "alice", PASSWORD)
    app_client.patch(
        "/api/profile",
        json={"ssh_username": "alice-deploy", "default_port": 2222},
        headers=csrf(alice_token),
    )

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        bob = bob_client.get("/api/profile").json()

        assert bob["ssh_username"] == "", "Bob must not inherit Alice's settings"
        assert bob["default_port"] == 22
        assert bob["user_id"] == users["bob"]

    # And Alice's own settings are intact.
    assert app_client.get("/api/profile").json()["ssh_username"] == "alice-deploy"


def test_profile_routes_take_no_user_identifier(app_client: TestClient, users) -> None:
    """The isolation mechanism is the route shape itself.

    There is no path or query parameter that could address another user, so a
    caller cannot even attempt it. This guards against a future refactor adding one.
    """
    sign_in(app_client, "alice", PASSWORD)
    schema = app_client.get("/openapi.json").json()

    profile_paths = [path for path in schema["paths"] if path.startswith("/api/profile")]
    assert profile_paths, "profile routes should exist"
    for path in profile_paths:
        assert "user_id" not in path
        assert "{user" not in path


def test_ssh_username_shell_metacharacters_are_rejected(
    app_client: TestClient, users
) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    response = app_client.patch(
        "/api/profile",
        json={"ssh_username": "deploy; rm -rf /"},
        headers=csrf(token),
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Private key handling
# --------------------------------------------------------------------------- #

def test_private_key_is_encrypted_at_rest(app_client: TestClient, users, settings) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    pem = _test_key()

    response = app_client.put(
        "/api/profile/private-key", json={"private_key": pem}, headers=csrf(token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["private_key_configured"] is True
    assert response.json()["private_key_type"] == "ssh-ed25519"

    stored = settings.private_key_dir / f"user-{users['alice']}.key"
    assert stored.exists()

    raw = stored.read_bytes()
    assert b"PRIVATE KEY" not in raw, "the key must not be stored in plaintext"
    assert b"BEGIN OPENSSH" not in raw
    assert raw.startswith(b"gAAAAA"), "expected Fernet ciphertext"


def test_private_key_never_appears_in_a_response(
    app_client: TestClient, users
) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    pem = _test_key()

    app_client.put(
        "/api/profile/private-key", json={"private_key": pem}, headers=csrf(token)
    )

    for path in ("/api/profile", "/api/auth/me", "/api/audit"):
        body = app_client.get(path).text
        assert "PRIVATE KEY" not in body, f"{path} leaked key material"
        assert pem.strip() not in body, f"{path} leaked the key verbatim"


def test_invalid_private_key_is_rejected_without_storing_anything(
    app_client: TestClient, users, settings
) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    response = app_client.put(
        "/api/profile/private-key",
        json={"private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n"},
        headers=csrf(token),
    )
    assert response.status_code == 400
    # The error must not echo the supplied text.
    assert "not-a-key" not in response.text
    assert not (settings.private_key_dir / f"user-{users['alice']}.key").exists()


def test_private_key_without_pem_marker_is_rejected(
    app_client: TestClient, users
) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    response = app_client.put(
        "/api/profile/private-key",
        json={"private_key": "just some text"},
        headers=csrf(token),
    )
    assert response.status_code == 422


def test_one_user_cannot_see_anothers_key_state(app_client: TestClient, users) -> None:
    alice_token = sign_in(app_client, "alice", PASSWORD)
    app_client.put(
        "/api/profile/private-key",
        json={"private_key": _test_key()},
        headers=csrf(alice_token),
    )

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        bob = bob_client.get("/api/profile").json()
        assert bob["secrets"]["private_key_configured"] is False
        assert bob["secrets"]["private_key_fingerprint"] is None


def test_deleting_a_private_key_removes_the_file(
    app_client: TestClient, users, settings
) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    app_client.put(
        "/api/profile/private-key", json={"private_key": _test_key()}, headers=csrf(token)
    )
    path = settings.private_key_dir / f"user-{users['alice']}.key"
    assert path.exists()

    response = app_client.delete("/api/profile/private-key", headers=csrf(token))
    assert response.status_code == 200
    assert response.json()["private_key_configured"] is False
    assert not path.exists()


# --------------------------------------------------------------------------- #
# Sudo password and passphrase
# --------------------------------------------------------------------------- #

def test_sudo_password_is_encrypted_and_never_returned(
    app_client: TestClient, users
) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    secret = "s3cret-sudo-pw"

    response = app_client.put(
        "/api/profile/sudo-password", json={"password": secret}, headers=csrf(token)
    )
    assert response.status_code == 200
    assert response.json()["sudo_password_configured"] is True
    assert secret not in response.text

    # Not in the profile response either.
    assert secret not in app_client.get("/api/profile").text

    session = get_session_factory()()
    try:
        stored = session.execute(
            __import__("sqlalchemy").text(
                "SELECT sudo_password_encrypted FROM user_ssh_secrets"
            )
        ).scalar_one()
    finally:
        session.close()

    assert stored and secret not in stored, "the password must be ciphertext"
    assert stored.startswith("gAAAAA")


def test_sudo_password_is_separate_from_the_key_passphrase(
    app_client: TestClient, users
) -> None:
    """They are distinct credentials and must not be shared storage."""
    from app.services import profiles

    token = sign_in(app_client, "alice", PASSWORD)
    app_client.put(
        "/api/profile/key-passphrase", json={"passphrase": "key-pass"}, headers=csrf(token)
    )
    app_client.put(
        "/api/profile/sudo-password", json={"password": "sudo-pass"}, headers=csrf(token)
    )

    assert profiles.load_passphrase(users["alice"]) == "key-pass"
    assert profiles.load_sudo_password(users["alice"]) == "sudo-pass"

    # Clearing one must not disturb the other.
    app_client.delete("/api/profile/key-passphrase", headers=csrf(token))
    assert profiles.load_passphrase(users["alice"]) is None
    assert profiles.load_sudo_password(users["alice"]) == "sudo-pass"


def test_sudo_password_is_per_user(app_client: TestClient, users) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    app_client.put(
        "/api/profile/sudo-password", json={"password": "alice-sudo"}, headers=csrf(token)
    )

    from app.services import profiles

    assert profiles.load_sudo_password(users["alice"]) == "alice-sudo"
    assert profiles.load_sudo_password(users["bob"]) is None


# --------------------------------------------------------------------------- #
# Executions are per-user
# --------------------------------------------------------------------------- #

def test_executions_require_authentication(app_client: TestClient) -> None:
    assert app_client.get("/api/executions/1").status_code == 401
    assert app_client.post("/api/executions/1/cancel").status_code == 401


def test_user_cannot_read_another_users_execution(
    app_client: TestClient, users
) -> None:
    """An execution id must not be a capability.

    Executions contain SSH output, so reading somebody else's run would leak
    exactly the data the suite is meant to keep private.
    """
    from app.services import execution_state
    from app.services.execution_state import HostSpec, StepSpec

    execution_id = execution_state.create_execution(
        workflow_name="alice-run",
        steps=[
            StepSpec(
                template_name="hello",
                template_version=1,
                script="echo hello\n",
                interpreter="bash",
                timeout_seconds=30,
            )
        ],
        hosts=[HostSpec(hostname="127.0.0.1", port=22, credential_id=1, credential_name="c")],
        created_by_id=users["alice"],
    )

    sign_in(app_client, "alice", PASSWORD)
    assert app_client.get(f"/api/executions/{execution_id}").status_code == 200

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        # 404, not 403: do not confirm that the id exists.
        assert bob_client.get(f"/api/executions/{execution_id}").status_code == 404


def test_user_cannot_cancel_another_users_execution(
    app_client: TestClient, users
) -> None:
    from app.services import execution_state
    from app.services.execution_state import HostSpec, StepSpec

    execution_id = execution_state.create_execution(
        workflow_name="alice-run",
        steps=[
            StepSpec(
                template_name="s",
                template_version=1,
                script="true\n",
                interpreter="bash",
                timeout_seconds=30,
            )
        ],
        hosts=[HostSpec(hostname="127.0.0.1", port=22, credential_id=1, credential_name="c")],
        created_by_id=users["alice"],
    )

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        token = bob_client.cookies.get("sshsuite_csrf") or ""
        response = bob_client.post(
            f"/api/executions/{execution_id}/cancel", headers=csrf(token)
        )
        assert response.status_code == 404


def test_cannot_start_an_execution_with_another_users_credential(
    app_client: TestClient, users
) -> None:
    """Ownership is checked before any SSH work begins."""
    from app.db.session import get_session_factory
    from app.models.credential import SshCredential

    session = get_session_factory()()
    try:
        credential = SshCredential(
            owner_id=users["alice"],
            name="alice-key",
            username="deploy",
            default_port=22,
            private_key_path="data/keys/999.key",
        )
        session.add(credential)
        session.commit()
        credential_id = credential.id
    finally:
        session.close()

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        token = bob_client.cookies.get("sshsuite_csrf") or ""
        response = bob_client.post(
            "/api/executions",
            json={
                "workflow_name": "steal",
                "credential_id": credential_id,
                "hosts": [{"hostname": "127.0.0.1", "port": 22}],
                "steps": [{"name": "s", "script": "echo hi\n"}],
            },
            headers=csrf(token),
        )
        # 400 with a "does not exist" message, so the response does not confirm
        # that another user's credential exists.
        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]


def test_credential_names_are_unique_per_owner(app_client: TestClient, users) -> None:
    """Two users may each have a credential with the same name."""
    from app.db.session import get_session_factory
    from app.models.credential import SshCredential

    session = get_session_factory()()
    try:
        for owner in (users["alice"], users["bob"]):
            session.add(
                SshCredential(
                    owner_id=owner,
                    name="prod-bastion",
                    username="deploy",
                    default_port=22,
                )
            )
        session.commit()

        count = session.query(SshCredential).filter_by(name="prod-bastion").count()
        assert count == 2
    finally:
        session.close()
def test_two_user_session_and_lock_lifecycle(app_client: TestClient, users) -> None:
    """The full two-user scenario, end to end, with independent cookie jars.
    Two separate ``TestClient`` instances stand in for two browsers. This covers the
    whole sequence at once because the interesting failures are cross-step: an
    isolation break often shows up as "A's logout affected B" or "B could touch A's
    lock" rather than as a single wrong status code.
    """
    import sqlite3

    from app.core.config import get_settings
    from app.models.permission import ResourceType
    from app.models.template import Template
    from app.services import locks
    alice_client = TestClient(app_client.app)
    bob_client = TestClient(app_client.app)
    login = alice_client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password-1"}
    )
    alice_token = login.json()["csrf_token"]
    bob_login = bob_client.post(
        "/api/auth/login", json={"username": "bob", "password": "bob-password-1"}
    )
    bob_token = bob_login.json()["csrf_token"]
    # 1. both authenticated at the same time, with distinct identities
    assert alice_client.get("/api/auth/me").json()["user"]["username"] == "alice"
    assert bob_client.get("/api/auth/me").json()["user"]["username"] == "bob"
    # 2. Alice stores a credential-ish secret (sudo password) and a private key state
    assert (
        alice_client.put(
            "/api/profile/sudo-password",
            json={"password": "alice-sudo-secret"},
            headers=csrf(alice_token),
        ).status_code
        == 200
    )
    # 3. Bob cannot see it; 4. nor change it
    bob_profile = bob_client.get("/api/profile")
    assert bob_profile.json()["user_id"] == users["bob"]
    assert bob_profile.json()["secrets"]["sudo_password_configured"] is False
    assert (
        bob_client.delete("/api/profile/sudo-password", headers=csrf(bob_token)).status_code
        == 200
    )
    # Alice's secret survived Bob's attempt to clear "his" (which was empty anyway)
    alice_profile = alice_client.get("/api/profile")
    assert alice_profile.json()["secrets"]["sudo_password_configured"] is True
    # 5/6. lock acquisition and release isolation
    session = get_session_factory()()
    try:
        template = Template(name="shared", owner_id=users["alice"], row_version=1)
        session.add(template)
        session.commit()
        template_id = template.id
    finally:
        session.close()
    acquired = alice_client.post(
        f"/api/locks/template/{template_id}", headers=csrf(alice_token)
    )
    assert acquired.status_code == 201
    held = bob_client.post(f"/api/locks/template/{template_id}", headers=csrf(bob_token))
    assert held.status_code == 403, "Bob has no view/edit share, so he cannot even contend"
    released = bob_client.delete(
        f"/api/locks/template/{template_id}", headers=csrf(bob_token)
    )
    assert released.status_code == 404, "Bob must not be able to release Alice's lock"
    assert locks.current_holder(
        ResourceType.TEMPLATE, template_id, user_id=users["alice"]
    ) is not None
    # 7/8/9. Alice logs out; her session dies, Bob's keeps working
    assert alice_client.post("/api/auth/logout", headers=csrf(alice_token)).status_code == 204
    assert alice_client.get("/api/auth/me").status_code == 401
    assert alice_client.get("/api/profile").status_code == 401
    assert bob_client.get("/api/auth/me").status_code == 200
    assert bob_client.get("/api/profile").status_code == 200
    # Alice's logout released her lock so another editor is not blocked for the TTL
    assert (
        locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=users["bob"]) is None
    )
    # Audit trail attributes the denial to Bob, by name and session.
    connection = sqlite3.connect(str(get_settings().sqlite_path))
    try:
        rows = connection.execute(
            "SELECT actor_user_id, actor_username, action, session_id "
            "FROM audit_events WHERE action = 'authz.access_denied' "
            "ORDER BY id DESC LIMIT 2"
        ).fetchall()
    finally:
        connection.close()
    assert rows, "expected denial events to be recorded"
    for actor_id, actor_username, _action, session_id in rows:
        assert actor_id == users["bob"]
        assert actor_username == "Bob"
        assert session_id is not None, "denials must carry the acting session"
