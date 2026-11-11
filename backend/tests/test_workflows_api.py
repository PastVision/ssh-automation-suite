"""Workflow manager HTTP surface: CRUD, locks, run, and secrecy.

These drive the real API through ``TestClient`` against a migrated database, so
they cover the layers the service tests cannot: request validation, status codes,
and -- most importantly for this feature -- that a Run-As password never appears
in a response.

The run endpoint is the only place a workflow becomes an execution, so the tests
here assert the bridge end to end: a workflow is authored, launched, and the
resulting execution is inspected through the executions API.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests import lab_support as lab
from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")
BOB = ("bob", "bob-password-1")


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


@pytest.fixture()
def bob(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *BOB)


def _template(client: TestClient, token: str, name: str = "t", script: str = "echo hi\n") -> int:
    response = client.post(
        "/api/templates",
        json={
            "name": name,
            "description": None,
            "visibility": "private",
            "steps": [
                {"name": "s1", "script": script, "interpreter": "bash", "timeout_seconds": 60}
            ],
        },
        headers=csrf(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _credential(user_id: int) -> int:
    """A credential owned by the signed-in user, with a real key file on disk."""
    from app.core.config import get_settings

    return lab.create_lab_credential(get_settings(), owner_id=user_id)


def _payload(
    *,
    name: str = "nightly",
    template_id: int,
    credential_id: int | None = None,
    entries: list[dict] | None = None,
    hosts: list[dict] | None = None,
    variables: list[dict] | None = None,
) -> dict:
    if entries is None:
        entries = [{"kind": "template", "template_id": template_id}]
    return {
        "name": name,
        "description": None,
        "credential_id": credential_id,
        "entries": entries,
        "hosts": hosts if hosts is not None else [{"hostname": "web-1.example.com"}],
        "variables": variables or [],
    }


def _create(client: TestClient, token: str, **kwargs):
    return client.post("/api/workflows", json=_payload(**kwargs), headers=csrf(token))


def _lock(client: TestClient, token: str, workflow_id: int) -> None:
    response = client.post(f"/api/locks/workflow/{workflow_id}", headers=csrf(token))
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


def test_create_read_and_list_round_trip(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    created = _create(client, token, template_id=template_id)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "nightly"
    assert body["row_version"] == 1
    assert len(body["entries"]) == 1
    assert body["entries"][0]["kind"] == "template"

    read = client.get(f"/api/workflows/{body['id']}", headers=csrf(token))
    assert read.status_code == 200
    assert read.json()["id"] == body["id"]

    listed = client.get("/api/workflows", headers=csrf(token))
    assert listed.status_code == 200
    assert [w["id"] for w in listed.json()["items"]] == [body["id"]]


def test_creating_a_workflow_requires_authentication(app_client, users):
    response = app_client.post(
        "/api/workflows",
        json=_payload(template_id=1),
    )
    assert response.status_code == 401


def test_update_requires_the_edit_lock(app_client, alice, users):
    """A write without the lock is refused before it can clobber an editor."""
    client, token = alice
    template_id = _template(client, token)
    workflow_id = _create(client, token, template_id=template_id).json()["id"]

    response = client.put(
        f"/api/workflows/{workflow_id}",
        json=_payload(template_id=template_id, name="renamed"),
        headers=csrf(token),
    )
    assert response.status_code == 423, response.text


def test_update_with_the_lock_renames_and_bumps_row_version(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    created = _create(client, token, template_id=template_id).json()
    _lock(client, token, created["id"])

    response = client.put(
        f"/api/workflows/{created['id']}",
        json=_payload(template_id=template_id, name="renamed"),
        headers=csrf(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "renamed"
    assert response.json()["row_version"] > created["row_version"]


def test_another_user_cannot_read_or_delete_my_workflow(app_client, alice, bob, users):
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow_id = _create(alice_client, alice_token, template_id=template_id).json()["id"]

    # A private workflow is invisible rather than forbidden: not-found reveals less.
    assert bob_client.get(
        f"/api/workflows/{workflow_id}", headers=csrf(bob_token)
    ).status_code == 404
    assert bob_client.delete(
        f"/api/workflows/{workflow_id}", headers=csrf(bob_token)
    ).status_code == 404


def test_delete_succeeds_with_the_lock_and_then_reads_404(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    workflow_id = _create(client, token, template_id=template_id).json()["id"]
    _lock(client, token, workflow_id)

    assert client.delete(
        f"/api/workflows/{workflow_id}", headers=csrf(token)
    ).status_code in (200, 204)
    assert client.get(
        f"/api/workflows/{workflow_id}", headers=csrf(token)
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Validation surface
# --------------------------------------------------------------------------- #


def test_a_blank_hostname_is_rejected(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    response = _create(client, token, template_id=template_id, hosts=[{"hostname": "  "}])
    assert response.status_code == 422, response.text


def test_a_draft_with_no_hosts_saves_but_cannot_run(app_client, alice, users):
    """Saving an incomplete workflow is deliberate: hosts are usually added after
    the steps. The refusal therefore belongs at launch, where it is a clear error
    rather than a silently empty run."""
    client, token = alice
    template_id = _template(client, token)
    credential_id = _credential(users["alice"])

    created = _create(
        client, token, template_id=template_id, credential_id=credential_id, hosts=[]
    )
    assert created.status_code == 201, created.text

    response = client.post(
        f"/api/workflows/{created.json()['id']}/run", json={}, headers=csrf(token)
    )
    assert response.status_code == 422, response.text
    assert "hosts" in response.json()["detail"].lower()


def test_an_unknown_template_is_a_400_not_a_500(app_client, alice, users):
    client, token = alice
    response = _create(client, token, template_id=999_999)
    assert response.status_code == 422, response.text
    assert "does not exist" in response.json()["detail"]


def test_an_unknown_credential_is_refused(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    response = _create(client, token, template_id=template_id, credential_id=999_999)
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------- #
# Secrecy
# --------------------------------------------------------------------------- #


def test_a_run_as_password_is_never_returned(app_client, alice, users):
    """The password is write-only. Echoing it back would put a secret in the
    response and in anything that logs one."""
    client, token = alice
    template_id = _template(client, token)
    secret = "VerySecretSudo9"

    response = _create(
        client,
        token,
        template_id=template_id,
        entries=[
            {
                "kind": "run_as",
                "target_username": "opspw",
                "method": "password",
                "password": secret,
            },
            {"kind": "template", "template_id": template_id},
        ],
    )
    assert response.status_code == 201, response.text
    assert secret not in response.text

    # And not on a re-read either.
    body = response.json()
    reread = client.get(f"/api/workflows/{body['id']}", headers=csrf(token))
    assert secret not in reread.text


def test_a_password_block_without_a_password_is_rejected(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    response = _create(
        client,
        token,
        template_id=template_id,
        entries=[
            {"kind": "run_as", "target_username": "opspw", "method": "password"},
            {"kind": "template", "template_id": template_id},
        ],
    )
    assert response.status_code == 422, response.text


def test_run_as_root_is_rejected(app_client, alice, users):
    """Run-As must never be a route to a root shell."""
    client, token = alice
    template_id = _template(client, token)
    response = _create(
        client,
        token,
        template_id=template_id,
        entries=[
            {"kind": "run_as", "target_username": "root", "method": "nopasswd"},
            {"kind": "template", "template_id": template_id},
        ],
    )
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------- #
# Run: the workflow -> execution bridge
# --------------------------------------------------------------------------- #


def test_running_a_workflow_creates_an_execution(app_client, alice, users):
    """The whole point of the milestone: author a workflow, launch it, and see a
    real execution row that the executions API can read back."""
    client, token = alice
    template_id = _template(client, token)
    credential_id = _credential(users["alice"])
    created = _create(
        client,
        token,
        template_id=template_id,
        credential_id=credential_id,
        hosts=[{"hostname": "127.0.0.1", "port": lab.lab_port(1)}],
    )
    assert created.status_code == 201, created.text

    launched = client.post(
        f"/api/workflows/{created.json()['id']}/run",
        json={},
        headers=csrf(token),
    )
    assert launched.status_code == 202, launched.text
    execution_id = launched.json()["execution_id"]
    assert execution_id > 0

    # The execution exists and is addressable through its own API.
    execution = client.get(f"/api/executions/{execution_id}", headers=csrf(token))
    assert execution.status_code == 200, execution.text
    body = execution.json()
    assert body["id"] == execution_id
    assert body["hosts"], "a launched workflow must have at least one host run"


def test_running_a_workflow_i_cannot_see_is_a_404(app_client, alice, bob, users):
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    credential_id = _credential(users["alice"])
    workflow_id = _create(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=credential_id,
    ).json()["id"]

    response = bob_client.post(
        f"/api/workflows/{workflow_id}/run", json={}, headers=csrf(bob_token)
    )
    assert response.status_code == 404


def test_running_without_a_credential_is_a_400(app_client, alice, users):
    """Better a clear 400 at the door than an execution that fails on every host."""
    client, token = alice
    template_id = _template(client, token)
    workflow_id = _create(client, token, template_id=template_id).json()["id"]

    response = client.post(
        f"/api/workflows/{workflow_id}/run", json={}, headers=csrf(token)
    )
    assert response.status_code == 422, response.text
    assert "credential" in response.json()["detail"].lower()


def test_a_host_selection_naming_no_known_host_is_a_400(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    credential_id = _credential(users["alice"])
    workflow_id = _create(
        client, token, template_id=template_id, credential_id=credential_id
    ).json()["id"]

    response = client.post(
        f"/api/workflows/{workflow_id}/run",
        json={"hostnames": ["not-in-this-workflow.example.com"]},
        headers=csrf(token),
    )
    assert response.status_code == 422, response.text
