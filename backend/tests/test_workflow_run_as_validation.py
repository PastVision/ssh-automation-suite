"""Save-time validation of Run-As credential availability.

A template step may ask for a sudo password, but a password is only *stored* on a
workflow Run-As block. Nothing used to connect the two at save time, so a workflow
could be saved in a shape that was already guaranteed to fail at execution -- and
the failure then arrived as an opaque sudo error on a remote host.

These tests pin the guarantee now made at save: a password-mode step must have a
block that both targets its account and holds a password. The counterpart at launch
lives in ``test_workflow_execution_specs``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests import lab_support as lab
from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


def _template(
    client: TestClient,
    token: str,
    *,
    name: str = "t",
    run_as_username: str | None = None,
    run_as_method: str | None = None,
) -> int:
    """A one-step template, optionally carrying its own Run-As identity."""
    step = {
        "name": "s1",
        "script": "id\n",
        "interpreter": "bash",
        "timeout_seconds": 60,
    }
    if run_as_username is not None:
        step["run_as_username"] = run_as_username
    if run_as_method is not None:
        step["run_as_method"] = run_as_method
    response = client.post(
        "/api/templates",
        json={"name": name, "description": None, "visibility": "private", "steps": [step]},
        headers=csrf(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _credential(user_id: int) -> int:
    from app.core.config import get_settings

    return lab.create_lab_credential(get_settings(), owner_id=user_id)


def _create(client: TestClient, token: str, *, credential_id: int, entries: list[dict]):
    return client.post(
        "/api/workflows",
        json={
            "name": "nightly",
            "description": None,
            "credential_id": credential_id,
            "entries": entries,
            "hosts": [{"hostname": "web-1.example.com"}],
            "variables": [],
        },
        headers=csrf(token),
    )


def _detail(response) -> dict:
    """The structured Run-As error body, asserting it is not a bare string."""
    body = response.json()["detail"]
    assert isinstance(body, dict), body
    return body


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #


def test_a_password_step_without_any_block_is_refused(app_client, alice, users):
    """The core hole: a step asked for a password nothing could ever supply."""
    client, token = alice
    template_id = _template(
        client, token, run_as_username="deploy", run_as_method="password"
    )

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=[{"kind": "template", "template_id": template_id}],
    )

    assert response.status_code == 422, response.text
    detail = _detail(response)
    assert detail["code"] == "run_as_credential_unavailable"
    assert detail["target_username"] == "deploy"
    assert "no Run-As block" in detail["message"]
    # The message names configuration, never a secret value.
    assert "password" in detail["message"].lower()


def test_a_step_that_names_only_a_user_never_requests_a_password(app_client, alice, users):
    """A username alone means passwordless, not "inherit whatever the block says".

    This is the resolution rule the executor already applies, pinned here so the new
    validation cannot start treating a username-only step as a password request and
    reject bodies that run fine.
    """
    client, token = alice
    template_id = _template(client, token, run_as_username="deploy")

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=[
            {
                "kind": "run_as",
                "target_username": "deploy",
                "method": "password",
                "password": "LabSudoPassword1",
            },
            {"kind": "template", "template_id": template_id},
        ],
    )

    assert response.status_code == 201, response.text


def test_a_password_step_before_any_block_is_refused(app_client, alice, users):
    """Order matters, and the blamed entry is the step, not the block."""
    client, token = alice
    template_id = _template(
        client, token, run_as_username="deploy", run_as_method="password"
    )

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=[
            {"kind": "template", "template_id": template_id},
            {
                "kind": "run_as",
                "target_username": "deploy",
                "method": "password",
                "password": "LabSudoPassword1",
            },
            {"kind": "template", "template_id": template_id},
        ],
    )

    assert response.status_code == 422, response.text
    # Entry 0 is the step that precedes the block, so nothing governs it.
    assert _detail(response)["entry_position"] == 0


def test_the_error_names_the_offending_entry_and_field(app_client, alice, users):
    """A client needs a location to point at, not just a banner."""
    client, token = alice
    good = _template(client, token, name="good")
    bad = _template(client, token, name="bad", run_as_username="ops", run_as_method="password")

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=[
            {"kind": "template", "template_id": good},
            {"kind": "template", "template_id": bad},
        ],
    )

    assert response.status_code == 422, response.text
    detail = _detail(response)
    assert detail["entry_position"] == 1
    assert detail["field"] == "run_as_method"
    assert "Step 2" in detail["message"]


# --------------------------------------------------------------------------- #
# Acceptances: the validation must not over-reach
# --------------------------------------------------------------------------- #


def test_a_step_that_declares_its_own_method_cannot_borrow_the_block_password(
    app_client, alice, users
):
    """Restating the method detaches the step from the block's transition.

    A password is fetched by block *position*, and a step with its own method has no
    position, so no password can reach it. Refusing at save is the point: the run
    would otherwise fail mid-transition on the host.
    """
    client, token = alice
    template_id = _template(
        client, token, run_as_username="deploy", run_as_method="password"
    )

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=[
            {
                "kind": "run_as",
                "target_username": "deploy",
                "method": "password",
                "password": "LabSudoPassword1",
            },
            {"kind": "template", "template_id": template_id},
        ],
    )

    assert response.status_code == 422, response.text
    detail = _detail(response)
    assert detail["entry_position"] == 1
    assert detail["field"] == "run_as_method"
    # The message must explain the fix, not just refuse.
    assert "inherit" in detail["message"].lower()


def test_a_block_supplies_the_method_when_the_step_only_sets_a_user(app_client, alice, users):
    """The common case: the step names a user and inherits the block's method."""
    client, token = alice
    template_id = _template(client, token, run_as_username="deploy")

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=[
            {
                "kind": "run_as",
                "target_username": "deploy",
                "method": "password",
                "password": "LabSudoPassword1",
            },
            {"kind": "template", "template_id": template_id},
        ],
    )

    assert response.status_code == 201, response.text


def test_a_passwordless_step_needs_no_block(app_client, alice, users):
    client, token = alice
    template_id = _template(
        client, token, run_as_username="deploy", run_as_method="nopasswd"
    )

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=[{"kind": "template", "template_id": template_id}],
    )

    assert response.status_code == 201, response.text


def test_a_step_with_no_run_as_at_all_is_unaffected(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=[{"kind": "template", "template_id": template_id}],
    )

    assert response.status_code == 201, response.text


def test_an_unsaved_workflow_needs_no_password_yet(app_client, alice, users):
    """A step with no Run-As identity cannot be unsatisfiable.

    This guards the validation against firing on the ordinary case of a workflow
    whose steps are all unprivileged.
    """
    client, token = alice
    template_id = _template(client, token)

    response = _create(
        client,
        token,
        credential_id=_credential(users["alice"]),
        entries=(
            [{"kind": "template", "template_id": template_id}] * 3
        ),
    )

    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# Update path
# --------------------------------------------------------------------------- #


def test_update_is_validated_the_same_way_as_create(app_client, alice, users):
    """A workflow must not be editable into a broken shape it could not be created in."""
    client, token = alice
    plain = _template(client, token, name="plain")
    needs_pw = _template(
        client, token, name="pw", run_as_username="deploy", run_as_method="password"
    )
    credential_id = _credential(users["alice"])

    created = _create(
        client,
        token,
        credential_id=credential_id,
        entries=[{"kind": "template", "template_id": plain}],
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]

    locked = client.post(f"/api/locks/workflow/{workflow_id}", headers=csrf(token))
    assert locked.status_code == 201, locked.text

    response = client.put(
        f"/api/workflows/{workflow_id}",
        json={
            "name": "nightly",
            "description": None,
            "credential_id": credential_id,
            "row_version": created.json()["row_version"],
            "entries": [{"kind": "template", "template_id": needs_pw}],
            "hosts": [{"hostname": "web-1.example.com"}],
            "variables": [],
        },
        headers=csrf(token),
    )

    assert response.status_code == 422, response.text
    assert _detail(response)["code"] == "run_as_credential_unavailable"
